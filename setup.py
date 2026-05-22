from setuptools import setup, find_packages

setup(
    name="eitelite",
    version="0.1.0",
    description="Minimal tical-code runtime — only the code that actually runs",
    packages=find_packages(exclude=["tests"]),
    python_requires=">=3.10",
    install_requires=[],  # zero external deps (stdlib only)
)
