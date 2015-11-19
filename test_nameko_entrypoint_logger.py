import json
import logging
import socket
from datetime import datetime

import pytest
from mock import ANY, MagicMock, Mock, patch
from nameko.constants import AMQP_URI_CONFIG_KEY
from nameko.containers import WorkerContext
from nameko.events import EventHandler, event_handler
from nameko.rpc import Rpc, rpc
from nameko.testing.services import entrypoint_hook, entrypoint_waiter
from nameko.testing.utils import DummyProvider, get_extension
from nameko.web.handlers import HttpRequestHandler, http
from werkzeug.test import create_environ
from werkzeug.wrappers import Request, Response

from nameko_entrypoint_logger import (
    EntrypointLogger, EntrypointLoggingHandler, dumps, get_http_request,
    get_worker_data, logging_dispatcher, process_response
)

EXCHANGE_NAME = "logging_exchange"
EVENT_TYPE = "monitoring_log"

dispatcher = MagicMock()


class CustomException(Exception):
    pass


class Service(object):
    name = "service"

    @rpc(expected_exceptions=CustomException)
    def rpc_method(self, foo):
        pass

    @http('GET', '/get/<int:value>')
    def get_method(self, request, value):
        payload = {'value': value}
        return json.dumps(payload)

    @event_handler("publisher", "property_updated")
    def handle_event(self, payload):
        pass


@pytest.fixture
def config():
    return {
        AMQP_URI_CONFIG_KEY: 'memory://',
        'ENTRYPOINT_LOGGING': {
            'EXCHANGE_NAME': EXCHANGE_NAME,
            'EVENT_TYPE': EVENT_TYPE
        }
    }


@pytest.fixture
def container(container_factory, config):
    return container_factory(Service, config)


@pytest.fixture
def entrypoint_logger(container):
    logger = EntrypointLogger().bind(container, "service")

    logger.setup()

    return logger


@pytest.fixture
def rpc_worker_ctx(entrypoint_logger):
    entrypoint = get_extension(
        entrypoint_logger.container, Rpc, method_name="rpc_method"
    )

    return WorkerContext(
        entrypoint_logger.container, Service, entrypoint, args=("bar",)
    )


@pytest.fixture
def http_entrypoint(entrypoint_logger):
    return get_extension(
        entrypoint_logger.container,
        HttpRequestHandler,
        method_name="get_method"
    )


@pytest.fixture
def http_worker_ctx(entrypoint_logger, http_entrypoint):
    environ = create_environ(
        '/get/1?test=123',
        'http://localhost:8080/',
        data=json.dumps({'foo': 'bar'}),
        content_type='application/json'
    )

    request = Request(environ)

    return WorkerContext(
        entrypoint_logger.container, Service, http_entrypoint, args=(request, 1)
    )


@pytest.fixture
def event_worker_ctx(entrypoint_logger):
    entrypoint = get_extension(
        entrypoint_logger.container, EventHandler, method_name="handle_event"
    )

    return WorkerContext(
        entrypoint_logger.container, Service, entrypoint, args=("bar",)
    )


@pytest.fixture
def supported_workers(rpc_worker_ctx, http_worker_ctx, event_worker_ctx):
    return [rpc_worker_ctx, http_worker_ctx, event_worker_ctx]


@pytest.fixture
def dummy_worker_ctx(mock_container):
    mock_container.service_name = "service"
    return WorkerContext(mock_container, None, DummyProvider())


def test_setup(entrypoint_logger):
    assert EntrypointLoggingHandler in [
        type(handler) for handler in entrypoint_logger.logger.handlers
        if type(handler) == EntrypointLoggingHandler]

    assert EXCHANGE_NAME in str(entrypoint_logger.container.config)
    assert EVENT_TYPE in str(entrypoint_logger.container.config)


def test_will_not_process_request_from_unknown_entrypoints(
    entrypoint_logger, dummy_worker_ctx
):
    with patch.object(entrypoint_logger, 'logger') as logger:
        entrypoint_logger.worker_setup(dummy_worker_ctx)

    assert not logger.info.called


def test_will_not_process_results_from_unknown_entrypoints(
    entrypoint_logger, dummy_worker_ctx
):
    with patch.object(entrypoint_logger, 'logger') as logger:
        entrypoint_logger.worker_result(dummy_worker_ctx)

    assert not logger.info.called


def test_requests_from_supported_workers_are_logged(
    entrypoint_logger, supported_workers
):
    with patch.object(entrypoint_logger, 'logger') as logger:
        with patch('nameko_entrypoint_logger.get_worker_data') as data:
            data.return_value = {'timestamp': datetime.utcnow()}
            for worker in supported_workers:
                entrypoint_logger.worker_setup(worker)
                (call_args,), _ = logger.info.call_args
                assert '"lifecycle_stage": "request"' in call_args

    assert logger.info.call_count == len(supported_workers)


def test_results_from_supported_workers_are_logged(
    entrypoint_logger, supported_workers
):
    with patch.object(entrypoint_logger, 'logger') as logger:
        with patch('nameko_entrypoint_logger.get_worker_data') as data:
            data.return_value = {'timestamp': datetime.utcnow()}
            with patch.object(
                entrypoint_logger, 'calculate_response_time'
            ) as response_time:
                response_time.return_value = 0.001
                for worker in supported_workers:
                    entrypoint_logger.worker_result(worker)
                    (call_args,), _ = logger.info.call_args
                    assert '"lifecycle_stage": "response"' in call_args

    assert logger.info.call_count == len(supported_workers)


def test_can_get_results_for_supported_workers(supported_workers):
    for worker in supported_workers:
        data = get_worker_data(worker)
        assert data['provider'] == type(worker.entrypoint).__name__
        assert data['hostname'] == socket.gethostname()
        assert data['service'] == worker.service_name
        assert data['provider_name'] == worker.entrypoint.method_name
        assert data['entrypoint'] == "{}.{}".format(
            data['service'], data['provider_name']
        )
        assert data['call_id'] == worker.call_id
        assert data['call_stack'] == worker.call_id_stack


def test_will_call_get_redacted_callargs(supported_workers):
    with patch('nameko_entrypoint_logger.get_redacted_args') as get_args:
        for worker in supported_workers:
            get_worker_data(worker)

    assert get_args.call_count == 2


def test_will_call_get_http_request(supported_workers):
    with patch('nameko_entrypoint_logger.get_http_request') as get_request:
        for worker in supported_workers:
            get_worker_data(worker)

    assert get_request.call_count == 1


def test_will_get_event_worker_redacted_callargs(event_worker_ctx):
    data = get_worker_data(event_worker_ctx)

    assert data['call_args'] == {
        'redacted_args': '{"payload": "bar"}'
    }


def test_will_get_rpc_worker_redacted_callargs(rpc_worker_ctx):
    data = get_worker_data(rpc_worker_ctx)

    assert data['call_args'] == {
        'redacted_args': '{"foo": "bar"}'
    }


@pytest.mark.parametrize(
    'result,result_serialized,result_bytes,status_code,content_type', [
        # can process dict result
        ({'foo': 'bar'}, '{"foo": "bar"}', 14, None, None),
        # can process string encoded dict result
        ("{'foo': 'bar'}", "{'foo': 'bar'}", 14, None, None),
        # can process string result
        ("foo=bar", 'foo=bar', 7, None, None),
        # can process None result
        (None, 'None', 4, None, None),
        # can process werkzeug's Response json result
        (Response(
            json.dumps({"value": 1}),
            mimetype='application/json'
        ), '{"value": 1}', 12, 200, 'application/json'),
        # can process werkzeug's Response text result
        (Response(
            "foo",
            mimetype='text/plain'
        ), 'foo', 3, 200, 'text/plain; charset=utf-8')
    ])
def test_can_process_results(
    result, result_serialized, result_bytes, status_code, content_type
):
    response = process_response(result)

    return_args = response['return_args']
    assert return_args['result'] == result_serialized
    assert return_args['result_bytes'] == result_bytes
    if status_code is not None:
        assert return_args['status_code'] == status_code
    if content_type is not None:
        assert return_args['content_type'] == content_type


@pytest.mark.parametrize('data,serialized_data,content_type', [
    (json.dumps({'foo': 'bar'}), '{"foo": "bar"}', 'application/json'),
    ('foo=bar', '{"foo": "bar"}', 'application/x-www-form-urlencoded'),
    ('foo=bar', 'foo=bar', 'text/plain')
])
def test_can_get_http_call_args(data, serialized_data, content_type):
    environ = create_environ(
        '/get/1?test=123',
        'http://localhost:8080/',
        data=data,
        content_type=content_type
    )

    request = Request(environ)

    request_call_args = get_http_request(request)
    assert request_call_args['data'] == serialized_data
    assert request_call_args['headers']['content_type'] == content_type


def test_entrypoint_logging_handler_will_dispatch_log_message():
    logger = logging.getLogger('test')
    handler = EntrypointLoggingHandler(dispatcher)
    logger.addHandler(handler)
    message = {'foo': 'bar'}
    logger.info(json.dumps(message))

    (call_args,), _ = dispatcher.call_args

    assert dispatcher.called
    assert json.loads(call_args) == message


def test_event_dispatcher_will_dispatch_logs(config):
    dispatcher = logging_dispatcher(config, EXCHANGE_NAME, EVENT_TYPE)

    message = {'foo': 'bar'}

    with patch('nameko_entrypoint_logger.producers') as mock_producers:
        with mock_producers[ANY].acquire(block=True) as mock_producer:
            dispatcher(json.dumps(message))

    (msg,), config = mock_producer.publish.call_args

    assert json.loads(msg) == message
    assert config['routing_key'] == EVENT_TYPE


def test_unexpected_exception_is_logged(entrypoint_logger, rpc_worker_ctx):
    exception = Exception("Something went wrong")
    exc_info = (Exception, exception, exception.__traceback__)

    entrypoint_logger.worker_timestamps[rpc_worker_ctx] = datetime.utcnow()

    with patch.object(entrypoint_logger, 'logger') as logger:
        entrypoint_logger.worker_result(
            rpc_worker_ctx, result={'bar': 'foo'}, exc_info=exc_info
        )

    (call_args,), _ = logger.info.call_args

    worker_data = json.loads(call_args)

    assert worker_data['provider'] == "Rpc"
    assert worker_data['expected_error'] == False
    assert worker_data['status'] == 'error'
    assert "Something went wrong" in str(worker_data['exc'])


def test_expected_exception_is_logged(entrypoint_logger, rpc_worker_ctx):
    exception = CustomException("Invalid value")
    exc_info = (CustomException, exception, exception.__traceback__)

    entrypoint_logger.worker_timestamps[rpc_worker_ctx] = datetime.utcnow()

    with patch.object(entrypoint_logger, 'logger') as logger:
        entrypoint_logger.worker_result(
            rpc_worker_ctx, result={'bar': 'foo'}, exc_info=exc_info
        )

    (call_args,), _ = logger.info.call_args

    worker_data = json.loads(call_args)

    assert worker_data['provider'] == "Rpc"
    assert worker_data['expected_error'] == True
    assert worker_data['status'] == 'error'
    assert "Invalid value" in str(worker_data['exc'])


def test_can_handle_failed_exception_repr(entrypoint_logger, rpc_worker_ctx):
    exception = ValueError("Invalid value")
    mock_exception = Mock()
    exc_info = (Exception, mock_exception, exception.__traceback__)

    entrypoint_logger.worker_timestamps[rpc_worker_ctx] = datetime.utcnow()

    with patch.object(entrypoint_logger, 'logger') as logger:
        entrypoint_logger.worker_result(
            rpc_worker_ctx, result={'bar': 'foo'}, exc_info=exc_info
        )

    (call_args,), _ = logger.info.call_args

    worker_data = json.loads(call_args)

    assert worker_data['exc'] == '[exc serialization failed]'


def test_end_to_end(container_factory, config):
    class TestService(object):
        name = "service"

        entrypoint_logger = EntrypointLogger()

        @rpc
        def rpc_method(self):
            pass

    container = container_factory(TestService, config)
    container.start()

    logger = get_extension(container, EntrypointLogger)

    with patch.object(logger, 'logger') as logger:
        with entrypoint_hook(container, 'rpc_method') as rpc_method:
            with entrypoint_waiter(container, 'rpc_method'):
                rpc_method()

    assert logger.info.call_count == 2


def test_default_json_serializer_will_raise_value_error():
    with pytest.raises(ValueError):
        dumps({'weird_value': {None}})


def test_can_handle_exception_when_getting_worker_data():
    worker_ctx = Mock()
    error_message = "Something went wrong."
    with patch('nameko_entrypoint_logger.hasattr') as hasattr_mock:
        hasattr_mock.side_effect = Exception(error_message)
        data = get_worker_data(worker_ctx)

    assert error_message in data['error']
