from setuptools import find_packages, setup


setup(
    name="futu-opend-execution",
    version="0.1.0",
    description="Hong Kong Futu/OpenD execution layer",
    package_dir={"": "src"},
    packages=find_packages(where="src"),
    python_requires=">=3.11",
    extras_require={
        "futu": ["futu-api"],
    },
)
