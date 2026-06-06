# Pre-import the real openshell._proto.openshell_pb2 so it lands in sys.modules
# before test_openshell_client.py's sys.modules.setdefault stubs can intercept it.
# conftest.py is loaded before any test file is imported, so this wins the race.
try:
    import openshell._proto.openshell_pb2  # noqa: F401
except ImportError:
    pass
