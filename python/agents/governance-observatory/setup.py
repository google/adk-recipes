from setuptools import setup, find_packages

setup(
    name="adk-governance-observatory",
    version="0.1.0",
    description="Runtime governance layer for Google Agent Development Kit",
    author="Akhilesh Warik",
    author_email="warikakhilesh319@gmail.com",
    license="Apache 2.0",
    packages=find_packages(where="src"),
    package_dir={"": "src"},
    install_requires=[
        "pydantic>=2.0.0",
        "click>=8.0.0"
    ],
    extras_require={
        "dev": ["pytest", "black", "ruff"]
    },
    entry_points={
        "console_scripts": [
            "adk-gov=src.cli.commands:cli",
        ],
    },
    classifiers=[
        "Development Status :: 3 - Alpha",
        "Intended Audience :: Developers",
        "License :: OSI Approved :: Apache Software License",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
    ],
    python_requires=">=3.9",
)