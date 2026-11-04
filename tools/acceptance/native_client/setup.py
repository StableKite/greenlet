from setuptools import setup,Extension
setup(name="greenlet-si-client",version="0",ext_modules=[Extension("_greenlet_si_client",["client.c"])])
