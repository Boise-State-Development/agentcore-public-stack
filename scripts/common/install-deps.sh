#!/bin/bash
# Dependency installer for CI/CD pipelines and local development
# Installs Node.js, AWS CDK CLI, Python, pip, and Docker (if needed)
# Usage: ./scripts/common/install-deps.sh [--skip-docker] [--skip-python] [--skip-node]

set -euo pipefail

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Default options
SKIP_DOCKER=false
SKIP_PYTHON=false
SKIP_NODE=false

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --skip-docker)
            SKIP_DOCKER=true
            shift
            ;;
        --skip-python)
            SKIP_PYTHON=true
            shift
            ;;
        --skip-node)
            SKIP_NODE=true
            shift
            ;;
        *)
            echo -e "${RED}Unknown option: $1${NC}"
            exit 1
            ;;
    esac
done

# Logging functions
log_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

log_section() {
    echo -e "\n${BLUE}===================================================${NC}"
    echo -e "${BLUE} $1${NC}"
    echo -e "${BLUE}===================================================${NC}\n"
}

# Detect OS
detect_os() {
    if [[ "$OSTYPE" == "linux-gnu"* ]]; then
        if [ -f /etc/os-release ]; then
            . /etc/os-release
            OS_NAME=$ID
        else
            OS_NAME="linux"
        fi
    elif [[ "$OSTYPE" == "darwin"* ]]; then
        OS_NAME="macos"
    elif [[ "$OSTYPE" == "msys" ]] || [[ "$OSTYPE" == "cygwin" ]]; then
        OS_NAME="windows"
    else
        OS_NAME="unknown"
    fi
    
    log_info "Detected OS: ${OS_NAME}"
}

# Check if command exists
command_exists() {
    command -v "$1" &> /dev/null
}

# Install Node.js
install_node() {
    if [ "$SKIP_NODE" = true ]; then
        log_info "Skipping Node.js installation (--skip-node flag)"
        return 0
    fi
    
    log_section "Installing Node.js"
    
    if command_exists node; then
        NODE_VERSION=$(node --version)
        log_info "Node.js already installed: ${NODE_VERSION}"
        
        # Check if version is acceptable (v18 or higher)
        MAJOR_VERSION=$(echo "$NODE_VERSION" | sed 's/v\([0-9]*\).*/\1/')
        if [ "$MAJOR_VERSION" -lt 18 ]; then
            log_warn "Node.js version ${NODE_VERSION} is older than recommended (v18+)"
        fi
    else
        log_info "Node.js not found. Installing..."
        
        case $OS_NAME in
            ubuntu|debian)
                curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
                sudo apt-get install -y nodejs
                ;;
            amzn)
                # Amazon Linux 2 or Amazon Linux 2023
                curl -fsSL https://rpm.nodesource.com/setup_20.x | sudo bash -
                sudo yum install -y nodejs
                ;;
            macos)
                if command_exists brew; then
                    brew install node@20
                else
                    log_error "Homebrew not found. Please install Node.js manually from https://nodejs.org/"
                    return 1
                fi
                ;;
            *)
                log_error "Automatic Node.js installation not supported on ${OS_NAME}"
                log_error "Please install Node.js manually from https://nodejs.org/"
                return 1
                ;;
        esac
        
        log_info "Node.js installed successfully"
    fi
    
    # Check npm
    if command_exists npm; then
        NPM_VERSION=$(npm --version)
        log_info "npm version: ${NPM_VERSION}"
    else
        log_error "npm not found after Node.js installation"
        return 1
    fi
}

# Install AWS CDK CLI
install_cdk() {
    log_section "Installing AWS CDK CLI"
    
    if command_exists cdk; then
        CDK_VERSION=$(cdk --version)
        log_info "AWS CDK already installed: ${CDK_VERSION}"
    else
        log_info "Installing AWS CDK CLI globally..."
        npm install -g aws-cdk
        log_info "AWS CDK installed successfully"
    fi
    
    # Verify installation
    if command_exists cdk; then
        CDK_VERSION=$(cdk --version)
        log_info "AWS CDK version: ${CDK_VERSION}"
    else
        log_error "CDK installation failed"
        return 1
    fi
}

# Install Python and pip
install_python() {
    if [ "$SKIP_PYTHON" = true ]; then
        log_info "Skipping Python installation (--skip-python flag)"
        return 0
    fi
    
    log_section "Installing Python"
    
    if command_exists python3; then
        PYTHON_VERSION=$(python3 --version)
        log_info "Python already installed: ${PYTHON_VERSION}"
    else
        log_info "Python 3 not found. Installing..."
        
        case $OS_NAME in
            ubuntu|debian)
                sudo apt-get update
                sudo apt-get install -y python3 python3-pip python3-venv
                ;;
            amzn)
                # Amazon Linux 2 or Amazon Linux 2023
                sudo yum install -y python3 python3-pip
                ;;
            macos)
                if command_exists brew; then
                    brew install python@3.11
                else
                    log_error "Homebrew not found. Please install Python manually"
                    return 1
                fi
                ;;
            *)
                log_error "Automatic Python installation not supported on ${OS_NAME}"
                return 1
                ;;
        esac
        
        log_info "Python installed successfully"
    fi
    
    # Check pip
    if command_exists pip3; then
        PIP_VERSION=$(pip3 --version)
        log_info "pip version: ${PIP_VERSION}"
    else
        log_warn "pip3 not found. Attempting to install..."
        python3 -m ensurepip --upgrade || true
    fi
}

# Install Docker
install_docker() {
    if [ "$SKIP_DOCKER" = true ]; then
        log_info "Skipping Docker installation (--skip-docker flag)"
        return 0
    fi
    
    log_section "Checking Docker"
    
    if command_exists docker; then
        DOCKER_VERSION=$(docker --version)
        log_info "Docker already installed: ${DOCKER_VERSION}"
        
        # Check if docker daemon is running
        if docker info &> /dev/null; then
            log_info "Docker daemon is running"
        else
            log_warn "Docker is installed but daemon is not running"
            log_warn "Please start Docker manually"
        fi
    else
        log_warn "Docker not found"
        log_warn "Docker is required for building container images"
        log_warn "Please install Docker manually from https://docs.docker.com/get-docker/"
        log_warn "Continuing without Docker..."
    fi
}

# Install jq for JSON parsing
install_jq() {
    log_section "Installing jq (JSON processor)"
    
    if command_exists jq; then
        JQ_VERSION=$(jq --version)
        log_info "jq already installed: ${JQ_VERSION}"
    else
        log_info "Installing jq..."
        
        case $OS_NAME in
            ubuntu|debian)
                sudo apt-get update
                sudo apt-get install -y jq
                ;;
            amzn)
                # Amazon Linux 2 or Amazon Linux 2023
                sudo yum install -y jq
                ;;
            macos)
                if command_exists brew; then
                    brew install jq
                else
                    log_warn "Homebrew not found. Skipping jq installation"
                    return 0
                fi
                ;;
            *)
                log_warn "Automatic jq installation not supported on ${OS_NAME}"
                return 0
                ;;
        esac
        
        log_info "jq installed successfully"
    fi
}

# Install AWS CLI (if not present)
install_aws_cli() {
    log_section "Checking AWS CLI"
    
    if command_exists aws; then
        AWS_VERSION=$(aws --version)
        log_info "AWS CLI already installed: ${AWS_VERSION}"
    else
        log_warn "AWS CLI not found"
        log_warn "AWS CLI is required for deployment"
        log_warn "Please install from https://aws.amazon.com/cli/"
    fi
}

# Main execution
main() {
    log_section "Dependency Installation Script"
    
    detect_os
    
    # Install dependencies in order
    install_node || { log_error "Node.js installation failed"; exit 1; }
    install_cdk || { log_error "CDK installation failed"; exit 1; }
    install_python || { log_warn "Python installation failed, continuing..."; }
    install_docker || { log_warn "Docker check failed, continuing..."; }
    install_jq || { log_warn "jq installation failed, continuing..."; }
    install_aws_cli || { log_warn "AWS CLI check failed, continuing..."; }
    
    log_section "Installation Complete"
    log_info "All required dependencies have been checked/installed"
    log_info "You can now proceed with building and deploying the application"
}

# Run main function
main
