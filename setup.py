from setuptools import find_packages, setup

setup(
    name='nbl-netbox-branching',
    version='0.2.0',
    description='A git-like branching implementation for NetBox',
    install_requires=[],
    packages=find_packages(),
    include_package_data=True,
    zip_safe=False,
)
