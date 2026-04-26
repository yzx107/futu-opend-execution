from setuptools import find_packages, setup


setup(
    name="futu-opend-execution",
    version="0.1.0",
    description="Hong Kong Futu/OpenD execution layer",
    package_dir={"": "src"},
    packages=find_packages(where="src"),
    package_data={
        "futu_opend_execution": ["web_static/*"],
    },
    python_requires=">=3.10",
    extras_require={
        "futu": ["futu-api"],
    },
)
