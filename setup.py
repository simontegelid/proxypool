#!/usr/bin/env python

try:
    from setuptools import setup
except ImportError:
    from distutils.core import setup

setup(name='Proxypool',
      version='0.1',
      description='A drop-in proxy wrapper for the Requests API',
      author='Simon Tegelid',
      author_email='simon@tegelid.se',
      packages=['proxypool'])
