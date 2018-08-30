import re

import requests


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
