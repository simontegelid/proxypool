#!/usr/bin/env python
import unittest
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from collections import Counter
import sys
if sys.version_info[0] < 3:
    # The socketserver backport in py2 doesn't include BaseServer
    # in __all__ so we import it like this instead
    import SocketServer as socketserver
else:
    import socketserver

import requests
import proxypool

call_stats = Counter()


class MyBaseServer(socketserver.BaseServer, object):
    def finish_request(self, request, client_address):
        import time
        time.sleep(self.latency)
        socketserver.BaseServer.finish_request(self, request, client_address)


class MyTCPServer(MyBaseServer, socketserver.TCPServer):
    def __init__(self, server_address, RequestHandlerClass, bind_and_activate=True, latency=0.0, response_code=200):
        self.latency = latency
        self.response_code = response_code
        socketserver.TCPServer.__init__(
            self, server_address, RequestHandlerClass, bind_and_activate)


class MyHTTPServer(MyTCPServer, HTTPServer):
    pass


class MyBaseHTTPRequestHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass


def for_all_methods(decorator):
    def decorate(cls):
        for attr in cls.__dict__:
            if callable(getattr(cls, attr)):
                setattr(cls, attr, decorator(getattr(cls, attr)))
        return cls
    return decorate


def add_call_statistics(func):
    def wrapper(*args, **kwargs):
        global call_stats
        self = args[0]
        call_stats["%s.%s.%d" % (
            self.__class__.__name__, func.__name__, self.server.server_port)] += 1
        return func(*args, **kwargs)
    return wrapper


@for_all_methods(add_call_statistics)
class HTTPRequestHandler(MyBaseHTTPRequestHandler):
    def _set_headers(self):
        self.send_response(self.server.response_code)
        self.send_header('Content-type', 'text/html')
        self.end_headers()

    def do_GET(self):
        self._set_headers()
        self.wfile.write(b"Got GET")

    def do_HEAD(self):
        self._set_headers()

    def do_POST(self):
        self._set_headers()
        self.wfile.write(b"Got POST")


@for_all_methods(add_call_statistics)
class HTTPProxyRequestHandler(MyBaseHTTPRequestHandler):
    def do_HEAD(self):
        self.do_GET(body=False)

    def do_GET(self, body=True):
        # Add some failure injection possiblities at first
        if self.server.response_code is None:
            # Don't send anything for testing purposes
            return
        elif self.server.response_code != 200:
            self.send_error(self.server.response_code)
            return

        try:
            resp = requests.get(self.path, headers=self.headers)
        except Exception as e:
            self.send_error(
                599, 'error proxying: {}'.format(str(e)))
        else:
            self.send_response(resp.status_code)
            for k, v in list(resp.headers.items()):
                self.send_header(k, v)
            self.end_headers()
            if body:
                self.wfile.write(resp.content)


class TestBase(unittest.TestCase):
    def setUp(self):
        self.servers = []

    def tearDown(self):
        for t, s in self.servers:
            s.server_close()
            s.shutdown()
            t.join()
        call_stats.clear()

    def spawn_servers(self, http_ports=[(8000, 0.0, 200, True, True)], http_proxy_ports=[(9000, 0.0, 200, True, True)]):
        for handler_class, conf in [(HTTPRequestHandler, http_ports),
                                    (HTTPProxyRequestHandler, http_proxy_ports)]:
            for port, latency, response_code, bind_and_activate in conf:
                server_address = ('', port)
                httpd = MyHTTPServer(
                    server_address, handler_class, bind_and_activate=bind_and_activate, latency=latency, response_code=response_code)
                t = threading.Thread(target=httpd.serve_forever)
                t.daemon = True
                t.start()

                self.servers.append((t, httpd))


class SelfTests(TestBase):
    """
    Veries the test harness itself, without invoking proxypool. 
    """

    def test_http_server(self):
        self.spawn_servers([(8000, 0.0, 200, True)],
                           [])

        r = requests.get('http://localhost:8000')

        self.assertTrue(r.text.endswith("Got GET"))
        self.assertEqual(call_stats['HTTPRequestHandler.do_GET.8000'], 1)

    def test_http_proxy_server(self):
        self.spawn_servers([(8000, 0.0, 200, True)],
                           [(9000, 0.0, 200, True)])

        proxies = {
            'http': 'http://localhost:9000',
        }

        r = requests.get('http://localhost:8000', proxies=proxies)

        self.assertTrue(r.text.endswith("Got GET"))
        self.assertEqual(call_stats['HTTPRequestHandler.do_GET.8000'], 1)
        self.assertEqual(
            call_stats['HTTPProxyRequestHandler.do_GET.9000'], 1)

    def test_http_proxy_servers(self):
        self.spawn_servers([(8000, 0.0, 200, True), (8001, 0.0, 200, True)],
                           [(9000, 0.0, 200, True), (9001, 0.0, 200, True)])

        requests.get('http://localhost:8001', proxies={
            'http': 'http://localhost:9000',
        })
        requests.get('http://localhost:8000', proxies={
            'http': 'http://localhost:9001',
        })

        self.assertEqual(call_stats['HTTPRequestHandler.do_GET.8000'], 1)
        self.assertEqual(
            call_stats['HTTPProxyRequestHandler.do_GET.9000'], 1)
        self.assertEqual(call_stats['HTTPRequestHandler.do_GET.8001'], 1)
        self.assertEqual(
            call_stats['HTTPProxyRequestHandler.do_GET.9001'], 1)


class ProxyPoolTests(TestBase):
    def test_various_latencies(self):
        self.spawn_servers([(8000, 0.0, 200, True)],
                           [(9000 + i, i / 1000., 200, True) for i in range(10)])

        class TestProvider(proxypool.ProxyProvider):
            def update(self):
                return set(['http://localhost:%d' % (9000 + i) for i in range(10)])

        num_calls = 1000

        pp = proxypool.ProxyPool(providers=[TestProvider()])

        for i in range(num_calls):
            pp.get('http://localhost:8000')

        self.assertEqual(
            call_stats['HTTPRequestHandler.do_GET.8000'], num_calls)
        self.assertTrue(call_stats['HTTPProxyRequestHandler.do_GET.9000']
                        > call_stats['HTTPProxyRequestHandler.do_GET.9001'], "Stochastic functions involved, so might fail occasionally...")

    def test_proxy_down(self):
        num_calls = 20
        num_working_proxies = 2
        num_bad_proxies = 1

        self.spawn_servers([(8000, 0.0, 200, True)],
                           [(9000 + i, 0.0, 200, True) for i in range(num_working_proxies)] +
                           [(5000 + i, 0.0, None, True) for i in range(num_bad_proxies)])

        class TestProvider(proxypool.ProxyProvider):
            def update(self):
                s = set(['http://localhost:%d' % (9000 + i) for i in range(num_working_proxies)] +
                        ['http://localhost:%d' % (5000 + i) for i in range(num_bad_proxies)])
                return s

        pp = proxypool.ProxyPool(providers=[TestProvider()])

        for i in range(num_calls):
            pp.get('http://localhost:8000')

        self.assertEqual(
            call_stats['HTTPRequestHandler.do_GET.8000'], num_calls)

        for i in range(num_bad_proxies):
            # Should be called once, and not respond, then never used again.
            self.assertEqual(
                call_stats['HTTPProxyRequestHandler.do_GET.%d' % (5000 + i)], 1)

        for i in range(num_working_proxies):
            # The working proxies should have been used more than once,
            # at least with very high probability.
            self.assertGreater(
                call_stats['HTTPProxyRequestHandler.do_GET.%d' % (9000 + i)], 1)


if __name__ == "__main__":
    unittest.main()
