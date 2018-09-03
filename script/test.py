#!/usr/bin/env python
import logging

import proxypool

logging.basicConfig(level=logging.DEBUG)

headers = {
    'User-Agent': "Mozilla/5.0 (compatible; U; ABrowse 0.6; Syllable) AppleWebKit/420+ (KHTML, like Gecko)",
    'Referer': 'my.original.path.net'}

pp = proxypool.ProxyPool(providers=[
    proxypool.providers.SocksProxy(),
    proxypool.providers.SslProxies(),
    proxypool.providers.GatherProxy(),
])

urls = [
    'https://api.ipify.org',
    'https://api.ipify.org?format=json',
    'http://ipv4bot.whatismyipaddress.com',
    'http://bot.whatismyipaddress.com',
    'http://ip-api.com/json',
]


def print_result(response):
    print("%s gave response %s" %
          (response.request.url, response.text))


tp = proxypool.ThreadPool(3)
tp.map(pp.get, [((url,), {'headers': headers})
                for url in urls], result_handler=print_result)
tp.wait()

print pp
