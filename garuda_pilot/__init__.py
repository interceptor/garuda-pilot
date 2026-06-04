from importlib.metadata import version, PackageNotFoundError

try:
    __version__ = version("garuda-pilot")
except PackageNotFoundError:
    __version__ = "dev"
