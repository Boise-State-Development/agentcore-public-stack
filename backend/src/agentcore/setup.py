from setuptools import setup, find_packages

# Since setup.py is inside the agentcore directory, we need to map the package
# name to the current directory. find_packages() will find subpackages.
found_packages = find_packages()
# Map 'agentcore' package to current directory ('.')
all_packages = ["agentcore"] + [f"agentcore.{pkg}" for pkg in found_packages]

setup(
    name="agentcore",
    version="0.1.0",
    packages=all_packages,
    package_dir={"agentcore": "."},
    install_requires=[
        "strands-agents==1.14.0",
        "strands-agents-tools==0.2.3",
        "boto3>=1.40.1",
        "python-dotenv>=1.0.0",
        "ddgs>=9.0.0",
        "aws-opentelemetry-distro>=0.10.1",
        "bedrock-agentcore",
        "nova-act==2.3.18.0",
    ],
)

