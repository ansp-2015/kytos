"""Kytos Controller (Kyco) is a main component of Kytos Project.

Kyco is used to handle low level openflow messages using python-openflow
library.
"""
from pkgutil import extend_path

__version__ = "1.1.0b1.dev1"
__path__ = extend_path(__path__, __name__)