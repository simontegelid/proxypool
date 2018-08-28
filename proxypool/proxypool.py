#!/usr/bin/env python
import re
import random
import threading
import logging
import time

import requests

logger = logging.getLogger(__name__)


class ProxyPool(object):
    """
    ProxyPool acts as a drop-in replacement for the requests API, enabling transparent
    connections through proxies. Proxies are statistically sampled based on their
    latency and failrate.

    Proxies are provided via ProxyProviders, typically scraping proxy lists on the web.
    Proxies are chosen at random, with higher probability of being sampled the lower
    latency the proxy has. Proxies causing a lot of connection failures are excluded
    from the pool if the failrate exceeds a configurable threshold. If all proxies become
    excluded, an update from the ProxyProviders is performed again.

    HTTP errors 503 and 429 are considered as probable rate-limits from the web hosting
    provider and a backoff of the entire pool is performed before new attempts are made.
    HTTP error 403 is considered as a blackist of the proxy by the web hosting provider,
    as the provided url is assumed to be correct. After a configurable amount of failures
    an exception is thrown to highlight that something might be erroneous with the reqest.  
    """

    class ProxyInst(object):
        """
        Represents a proxy combined with its metrics (latency, failrate, etc)
        """

        def __init__(self, ppref, url):
            """
            :url: eg. "{http,https,socks4,socks5}://37.59.8.29:39867"
            """
            self.proxypool = ppref  # Needed to lock pool for concurrency
            self.url = url
            # Assume an initially small latency to give the proxy high
            # probability of being sampled.
            self.t = 1e-3
            self.w = 1 / self.t
            self.failures = 0
            self.successes = 0
            self.down = False
            self.sample_counter = 0

        def __str__(self):
            return "%s: latency: %f (%d/%d=%.2f failrate)" % (self.url, self.t,
                                                              self.failures,
                                                              self.failures + self.successes,
                                                              self.failrate())

        def as_dict(self):
            """
            Provides a requests friendly representation of the proxy.
            Assumes that HTTP proxies also can handle HTTPS.
            """
            return {'http': self.url.replace('https://', 'http://'),
                    'https': self.url.replace('http://', 'https://')}

        def failrate(self):
            """
            Returns the failrate of the proxy in the range [0,1).
            """
            return self.failures / (self.successes + self.failures + 1e-7)

        def set_latency(self, t):
            """
            :t: response time in seconds
            """
            with self.proxypool.lock:
                self.t = t

        def increase_successes(self):
            with self.proxypool.lock:
                self.successes += 1

        def increase_failures(self):
            with self.proxypool.lock:
                self.failures += 1

        def set_down(self):
            with self.proxypool.lock:
                self.down = True

    class Decorators(object):
        @classmethod
        def with_proxypool(cls, apifunc):
            def proxypool_caller(*args, **kwargs):
                self = args[0]
                failures = 0

                while True:
                    p = self.get_proxy()
                    logger.info("Using %s" % p)
                    kwargs['proxies'] = p.as_dict()
                    kwargs['timeout'] = self.default_timeout
                    try:
                        r = apifunc(*args, **kwargs)
                    except (requests.exceptions.ConnectionError,
                            requests.exceptions.ChunkedEncodingError,
                            requests.exceptions.ReadTimeout) as e:
                        logger.debug("%s: %s" % (p.url, e))
                        p.increase_failures()
                        failures += 1
                    else:
                        if r.status_code in [403]:
                            logger.debug(
                                "%s: Down due to http status %d" %
                                (p.url, r.status_code))
                            # The proxy is assumed to be banned, so mark it as
                            # down immediately
                            p.set_down()
                            failures += 1
                        elif r.status_code in [429, 503]:
                            logger.debug(
                                "%s: Probable rate limit due to http status %d" %
                                (p.url, r.status_code))
                            logger.debug(r.headers)
                            time.sleep(60)
                            # TODO: penalize only affected proxy and not entire
                            # pool
                            # TODO: Handle Retry-After
                        else:
                            logger.debug(
                                "%s: Latency %.2f sec" %
                                (p.url, r.elapsed.total_seconds()))
                            p.set_latency(r.elapsed.total_seconds())
                            p.increase_successes()
                            return r

                    if failures > self.max_proxy_attempts:
                        raise Exception(
                            "Too many failures, probably bad request (%s)" %
                            kwargs)
            return proxypool_caller

    def __init__(self, providers, connection_retries=30, default_timeout=5.0, max_proxy_failrate=0.1):
        """
        :providers: List of ProxyProvider instances that are used for providing proxies
        :connection_retries: Number of attempts to get an URL, via different proxies, 
            before raising an exception
        :default_timeout: Connection timeout, passed to requests as a 'timeout' argument
        :max_proxy_failrate: Failure limit of a proxy before considering it bad and stop using it
        """
        self.providers = providers
        self.proxies = set()
        self.lock = threading.Lock()
        self.max_proxy_attempts = connection_retries
        self.default_timeout = default_timeout
        self.max_proxy_failrate = max_proxy_failrate
        self.provider_updates = 0

    def __str__(self):
        with self.lock:
            gp = self.__good_proxies()
        return "%d proxies (%d good, updated %d times)" % (
            len(self.proxies), len(gp), self.provider_updates)

    def __len__(self):
        return len(self.proxies)

    @staticmethod
    def __set_weights(gp):
        """
        Must be called inside lock
        """
        for i in range(len(gp)):
            gp[i].w = gp[i].t ** -1.0
        s = sum([x.w for x in gp])
        for i in range(len(gp)):
            gp[i].w = gp[i].w / s

    def __fetch_proxies(self):
        """
        Must be called inside lock
        """
        self.proxies = set()
        for pr in self.providers:
            proxies = pr.update()
            logger.info(
                "Got %d proxies from %s" %
                (len(proxies), pr.__class__))
            for proxy in proxies:
                proxy_inst = ProxyPool.ProxyInst(self, proxy)
                self.proxies.add(proxy_inst)
        self.provider_updates += 1
        if len(self.proxies) == 0:
            raise Exception("No proxies provided from any provider")

    def __good_proxies(self):
        """
        Must be called inside lock
        """
        return [s for s in self.proxies if s.failrate() < self.max_proxy_failrate and not s.down]

    def get_proxy(self):
        """ Sample a good proxy """
        with self.lock:
            r = random.uniform(0, 1)
            gp = self.__good_proxies()
            if len(gp) == 0:
                self.__fetch_proxies()
                gp = list(self.proxies)
            self.__set_weights(gp)
            s = gp[0].w
            i = 0

            while s < r:
                i += 1
                s += gp[i].w

            p = gp[i]
            p.sample_counter += 1
        return p

    @Decorators.with_proxypool
    def request(self, *args, **kwargs):
        return requests.api.request(*args, **kwargs)

    @Decorators.with_proxypool
    def get(self, *args, **kwargs):
        return requests.api.get(*args, **kwargs)

    @Decorators.with_proxypool
    def options(self, *args, **kwargs):
        return requests.api.options(*args, **kwargs)

    @Decorators.with_proxypool
    def head(self, *args, **kwargs):
        return requests.api.head(*args, **kwargs)

    @Decorators.with_proxypool
    def post(self, *args, **kwargs):
        return requests.api.post(*args, **kwargs)

    @Decorators.with_proxypool
    def put(self, *args, **kwargs):
        return requests.api.put(*args, **kwargs)

    @Decorators.with_proxypool
    def patch(self, *args, **kwargs):
        return requests.api.patch(*args, **kwargs)

    @Decorators.with_proxypool
    def delete(self, *args, **kwargs):
        return requests.api.delete(*args, **kwargs)


class ProxyProvider(object):
    """
    Provides proxies, eg. "socks5://1.1.1.1:5000"
    """

    def update(self):
        raise NotImplementedError


class SocksProxy(ProxyProvider):

    def update(self):
        r = requests.get('https://www.socks-proxy.net/')
        t = r.text

        ps = set()
        for m in re.findall(
            r"<tr><td>(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})<\/td><td>(\d+)<\/td><td>.*?<\/td><td class='hm'>.*?<\/td><td>(\w+?)<\/td>",
                t):
            ip = m[0]
            port = m[1]
            prot = m[2].lower()
            assert prot in ["http", "https", "socks4", "socks5"]
            ps.add("%s://%s:%s" % (prot, ip, port))
        return ps


class SslProxies(ProxyProvider):

    def update(self):
        r = requests.get('https://www.sslproxies.org/')
        t = r.text

        ps = set()
        for m in re.findall(
            r"<tr><td>(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})<\/td><td>(\d+)<\/td>",
                t):
            ip = m[0]
            port = m[1]
            prot = "https"
            assert prot in ["http", "https", "socks4", "socks5"]
            ps.add("%s://%s:%s" % (prot, ip, port))
        return ps


class Tor(ProxyProvider):

    def update(self):
        return set(['socks5://localhost:9050'])


all_providers = [SocksProxy, SslProxies, Tor]

if __name__ == "__main__":
    try:
        import Queue as queue
    except ImportError:
        import queue as queue

    logging.basicConfig(level=logging.DEBUG)
    headers = {
        'User-Agent': "Mozilla/5.0 (compatible; U; ABrowse 0.6; Syllable) AppleWebKit/420+ (KHTML, like Gecko)",
        'Referer': 'my.original.path.net'}

    proxy_pool = ProxyPool(providers=[
        SocksProxy(),
        SslProxies(),
    ])

    urls = [
        'https://api.ipify.org',
        'https://api.ipify.org?format=json',
        'http://ipv4bot.whatismyipaddress.com',
        'http://bot.whatismyipaddress.com',
        'http://ip-api.com/json',
    ]

    def worker(url, proxy_pool, rq):
        for i in range(10):
            r = proxy_pool.get(url,
                               headers=headers)
            rq.put(r)

    rq = queue.Queue()

    ts = []
    for url in urls:
        t = threading.Thread(target=worker, args=(url, proxy_pool, rq))
        t.start()
        ts.append(t)
    for t in ts:
        t.join()
    while True:
        try:
            r = rq.get_nowait()
            print r.text
        except queue.Empty:
            break

    print proxy_pool
