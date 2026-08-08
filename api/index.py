"""Vercel entry point.

Vercel looks for an `app` object in this file and serves it as a WSGI
application. Everything else lives in the project root, one level up.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app  # noqa: E402,F401
