"""Setup configuration for superTrader project"""

from setuptools import setup, find_packages

with open("README.md", "r", encoding="utf-8") as fh:
    long_description = fh.read()

setup(
    name="superTrader",
    version="4.0.0",
    author="Huang Liang",
    description="Systematic trading framework with ML-based regime detection and intraday defense mechanisms",
    long_description=long_description,
    long_description_content_type="text/markdown",
    url="https://github.com/yourusername/superTrader",
    packages=find_packages(where="src"),
    package_dir={"": "src"},
    python_requires=">=3.9",
    install_requires=[
        "pandas>=1.5.0",
        "numpy>=1.23.0",
        "akshare>=1.14.0",
        "pydantic>=2.0.0",
        "pyyaml>=6.0",
        "requests>=2.28.0",
        "ta>=0.10.0",
        "PyQt6>=6.4.0",
        "sklearn>=0.0",
        "optuna>=3.0.0",
    ],
    extras_require={
        "dev": [
            "pytest>=7.0.0",
            "pytest-cov>=4.0.0",
            "pytest-mock>=3.10.0",
            "black>=23.0.0",
            "pylint>=2.15.0",
            "mypy>=0.990",
            "isort>=5.11.0",
            "flake8>=5.0.0",
            "bandit>=1.7.0",
            "safety>=2.3.0",
            "sphinx>=5.0.0",
            "sphinx-rtd-theme>=1.1.0",
        ],
        "ci": [
            "pytest>=7.0.0",
            "pytest-cov>=4.0.0",
            "black>=23.0.0",
            "pylint>=2.15.0",
            "mypy>=0.990",
            "bandit>=1.7.0",
        ],
    },
    entry_points={
        "console_scripts": [
            "supertrader=src.main:main",
        ],
    },
    classifiers=[
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "License :: OSI Approved :: MIT License",
        "Operating System :: OS Independent",
        "Topic :: Office/Business :: Financial :: Investment",
        "Development Status :: 4 - Beta",
    ],
)
