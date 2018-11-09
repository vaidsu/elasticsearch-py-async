#!/bin/python
# Inspired from
# https://gist.github.com/amitripshtos/efd280e88376623b491c8682f417d597
from __future__ import unicode_literals

import logging
from operator import methodcaller
import asyncio

from elasticsearch.exceptions import TransportError
from elasticsearch.helpers import BulkIndexError
from elasticsearch.compat import map, string_types

logger = logging.getLogger('elasticsearch.helpers')


def _async_expand_action(data):
    """
    From one document or action definition passed in by the user extract the
    action/data lines needed for elasticsearch's
    :meth:`~elasticsearch.Elasticsearch.bulk` api.
    """
    # when given a string, assume user wants to index raw json
    if isinstance(data, string_types):
        return '{"index":{}}', data

    # make sure we don't alter the action
    data = data.copy()
    op_type = data.pop('_op_type', 'index')
    action = {op_type: {}}
    for key in ('_index', '_parent', '_percolate', '_routing', '_timestamp',
                'routing', '_type', '_version', '_version_type', '_id',
                'retry_on_conflict', 'pipeline'):
        if key in data:
            action[op_type][key] = data.pop(key)

    # no data payload for delete
    if op_type == 'delete':
        return action, None

    return action, data.get('_source', data)


async def _async_chunk_actions(actions, chunk_size, max_chunk_bytes,
                               serializer):
    """
    Split actions into chunks by number or size, serialize them into strings in
    the process.
    """
    bulk_actions, bulk_data = [], []
    size, action_count = 0, 0
    async for user_action in actions():
        action, data = _async_expand_action(user_action)
        raw_data, raw_action = data, action
        action = serializer.dumps(action)
        cur_size = len(action) + 1

        if data is not None:
            data = serializer.dumps(data)
            cur_size += len(data) + 1

        # full chunk, send it and start a new one
        if bulk_actions and (size + cur_size > max_chunk_bytes
                             or action_count == chunk_size):
            yield bulk_data, bulk_actions
            bulk_actions, bulk_data = [], []
            size, action_count = 0, 0

        bulk_actions.append(action)
        if data is not None:
            bulk_actions.append(data)
            bulk_data.append((raw_action, raw_data))
        else:
            bulk_data.append((raw_action, ))

        size += cur_size
        action_count += 1

    if bulk_actions:
        yield bulk_data, bulk_actions


async def _process_bulk_chunk(client,
                              bulk_actions,
                              bulk_data,
                              raise_on_exception=True,
                              raise_on_error=True,
                              *args,
                              **kwargs):
    """
    Send a bulk request to elasticsearch and process the output.
    """
    # if raise on error is set, we need to collect errors per chunk before raising them
    errors = []

    try:
        # send the actual request
        resp = await client.bulk('\n'.join(bulk_actions) + '\n', *args,
                                 **kwargs)
    except TransportError as e:
        # default behavior - just propagate exception
        if raise_on_exception:
            raise e

        # if we are not propagating, mark all actions in current chunk as failed
        err_message = str(e)
        exc_errors = []

        for data in bulk_data:
            # collect all the information about failed actions
            op_type, action = data[0].copy().popitem()
            info = {
                "error": err_message,
                "status": e.status_code,
                "exception": e
            }
            if op_type != 'delete':
                info['data'] = data[1]
            info.update(action)
            exc_errors.append({op_type: info})

        # emulate standard behavior for failed actions
        if raise_on_error:
            raise BulkIndexError(
                '%i document(s) failed to index.' % len(exc_errors),
                exc_errors)
        else:
            for err in exc_errors:
                yield False, err
            return

    # go through request-reponse pairs and detect failures
    for data, (op_type,
               item) in zip(bulk_data,
                            map(methodcaller('popitem'), resp['items'])):
        ok = 200 <= item.get('status', 500) < 300
        if not ok and raise_on_error:
            # include original document source
            if len(data) > 1:
                item['data'] = data[1]
            errors.append({op_type: item})

        if ok or not errors:
            # if we are not just recording all errors to be able to raise
            # them all at once, yield items individually
            yield ok, {op_type: item}

    if errors:
        raise BulkIndexError('%i document(s) failed to index.' % len(errors),
                             errors)


async def streaming_bulk(client,
                         actions,
                         chunk_size=500,
                         max_chunk_bytes=100 * 1024 * 1024,
                         raise_on_error=True,
                         raise_on_exception=True,
                         max_retries=0,
                         initial_backoff=2,
                         max_backoff=600,
                         yield_ok=True,
                         *args,
                         **kwargs):
    """
    Streaming bulk consumes actions from the iterable passed in and yields
    results per action. For non-streaming usecases use
    :func:`~elasticsearch.helpers.bulk` which is a wrapper around streaming
    bulk that returns summary information about the bulk operation once the
    entire input is consumed and sent.

    If you specify ``max_retries`` it will also retry any documents that were
    rejected with a ``429`` status code. To do this it will wait (**by calling
    time.sleep which will block**) for ``initial_backoff`` seconds and then,
    every subsequent rejection for the same chunk, for double the time every
    time up to ``max_backoff`` seconds.

    :arg client: instance of :class:`~elasticsearch.Elasticsearch` to use
    :arg actions: iterable containing the actions to be executed
    :arg chunk_size: number of docs in one chunk sent to es (default: 500)
    :arg max_chunk_bytes: the maximum size of the request in bytes (default: 100MB)
    :arg raise_on_error: raise ``BulkIndexError`` containing errors (as `.errors`)
        from the execution of the last chunk when some occur. By default we raise.
    :arg raise_on_exception: if ``False`` then don't propagate exceptions from
        call to ``bulk`` and just report the items that failed as failed.
    :arg max_retries: maximum number of times a document will be retried when
        ``429`` is received, set to 0 (default) for no retries on ``429``
    :arg initial_backoff: number of seconds we should wait before the first
        retry. Any subsequent retries will be powers of ``initial_backoff *
        2**retry_number``
    :arg max_backoff: maximum number of seconds a retry will wait
    :arg yield_ok: if set to False will skip successful documents in the output
    """
    async for bulk_data, bulk_actions in _async_chunk_actions(
            actions, chunk_size, max_chunk_bytes, client.transport.serializer):

        for attempt in range(max_retries + 1):
            to_retry, to_retry_data = [], []
            if attempt:
                await asyncio.sleep(
                    min(max_backoff, initial_backoff * 2**(attempt - 1)))

            try:
                async for ok, info in _process_bulk_chunk(
                        client, bulk_actions, bulk_data, raise_on_exception,
                        raise_on_error, *args, **kwargs):
                    if not ok:
                        action, info = info.popitem()
                        # retry if retries enabled, we get 429, and we are not
                        # in the last attempt
                        if max_retries \
                                and info['status'] == 429 \
                                and (attempt + 1) <= max_retries:
                            # _process_bulk_chunk expects strings so we need to
                            # re-serialize the data
                            to_retry.extend(
                                map(client.transport.serializer.dumps,
                                    bulk_data))
                            to_retry_data.append(bulk_data)
                        else:
                            yield ok, {action: info}
                    elif yield_ok:
                        yield ok, info

            except TransportError as e:
                # suppress 429 errors since we will retry them
                if attempt == max_retries or e.status_code != 429:
                    raise
            else:
                if not to_retry:
                    break
                # retry only subset of documents that didn't succeed
                bulk_actions, bulk_data = to_retry, to_retry_data


async def bulk(client, actions, stats_only=False, *args, **kwargs):
    """
    Helper for the :meth:`~elasticsearch.Elasticsearch.bulk` api that provides
    a more human friendly interface - it consumes an iterator of actions and
    sends them to elasticsearch in chunks. It returns a tuple with summary
    information - number of successfully executed actions and either list of
    errors or number of errors if ``stats_only`` is set to ``True``. Note that
    by default we raise a ``BulkIndexError`` when we encounter an error so
    options like ``stats_only`` only apply when ``raise_on_error`` is set to
    ``False``.

    When errors are being collected original document data is included in the
    error dictionary which can lead to an extra high memory usage. If you need
    to process a lot of data and want to ignore/collect errors please consider
    using the :func:`~elasticsearch.helpers.streaming_bulk` helper which will
    just return the errors and not store them in memory.


    :arg client: instance of :class:`~elasticsearch.Elasticsearch` to use
    :arg actions: iterator containing the actions
    :arg stats_only: if `True` only report number of successful/failed
        operations instead of just number of successful and a list of error responses

    Any additional keyword arguments will be passed to
    :func:`~elasticsearch.helpers.streaming_bulk` which is used to execute
    the operation, see :func:`~elasticsearch.helpers.streaming_bulk` for more
    accepted parameters.
    """
    success, failed = 0, 0

    # list of errors to be collected is not stats_only
    errors = []

    # make streaming_bulk yield successful results so we can count them
    kwargs['yield_ok'] = True
    async for ok, item in streaming_bulk(client, actions, *args, **kwargs):
        # go through request-reponse pairs and detect failures
        if not ok:
            if not stats_only:
                errors.append(item)
            failed += 1
        else:
            success += 1

    return success, failed if stats_only else errors
