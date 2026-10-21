"""Reuse the existing protocol tests, without Windows registry/mutex/GUI tests."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'tests'))
import test_campus_keepalive as existing


def load_tests(loader, tests, pattern):
    suite = unittest.TestSuite()
    for name in ('ParsingTests', 'EngineTests', 'ProtocolTests', 'WorkerTests'):
        suite.addTests(loader.loadTestsFromTestCase(getattr(existing, name)))
    return suite
