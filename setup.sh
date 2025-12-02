#!/bin/bash

echo "🚀 Setting up AgentCore Public Stack..."

# Check if Python is installed
if ! command -v python3 &> /dev/null; then
    echo "❌ Python 3 is not installed. Please install Python 3.8 or higher."
    exit 1
fi

# Check if Node.js is installed
if ! command -v node &> /dev/null; then
    echo "❌ Node.js is not installed. Please install Node.js 18 or higher."
    exit 1
fi

# Check if npm is installed
if ! command -v npm &> /dev/null; then
    echo "❌ npm is not installed. Please install npm."
    exit 1
fi

echo "✅ Prerequisites check passed"

# Check for AWS CLI and profile configuration
echo ""
echo "🔍 Checking AWS configuration..."
if command -v aws &> /dev/null; then
    echo "✅ AWS CLI found"

    # Check if .env file exists to read AWS_PROFILE
    if [ -f "backend/src/.env" ]; then
        AWS_PROFILE_FROM_ENV=$(grep '^AWS_PROFILE=' backend/src/.env | cut -d '=' -f2 | tr -d ' ')
    fi

    # Use AWS_PROFILE from environment, .env file, or default
    PROFILE_TO_USE="${AWS_PROFILE:-${AWS_PROFILE_FROM_ENV:-default}}"

    if [ "$PROFILE_TO_USE" != "default" ]; then
        echo "Using AWS profile: $PROFILE_TO_USE"
        if aws configure list --profile "$PROFILE_TO_USE" &> /dev/null; then
            echo "✅ AWS profile '$PROFILE_TO_USE' is configured"
            export AWS_PROFILE="$PROFILE_TO_USE"
        else
            echo "⚠️  AWS profile '$PROFILE_TO_USE' not found, will use default credentials"
            unset AWS_PROFILE
        fi
    else
        echo "Using default AWS credentials"
        # Check if any AWS credentials are configured
        if aws configure list &> /dev/null 2>&1; then
            echo "✅ AWS credentials configured"
        else
            echo "⚠️  No AWS credentials found - some features may not work"
            echo "   Run 'aws configure' to set up credentials"
        fi
    fi
else
    echo "⚠️  AWS CLI not found - some features may require AWS credentials"
    echo "   Install from: https://aws.amazon.com/cli/"
fi

echo ""
# Install backend dependencies
echo "📦 Installing backend dependencies..."
cd backend

if [ ! -d "venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv venv
fi

echo "Activating virtual environment..."
source venv/bin/activate

echo "Upgrading pip..."
./venv/bin/python -m pip install --upgrade pip

echo "Installing agentcore-stack package with all dependencies..."
./venv/bin/python -m pip install -e ".[agentcore,dev]"

if [ $? -eq 0 ]; then
    echo "✅ Backend dependencies installed successfully"
    deactivate
else
    echo "❌ Failed to install backend dependencies"
    deactivate
    exit 1
fi

cd ..

# Install frontend dependencies
echo "📦 Installing frontend dependencies..."
cd frontend/ai.client
npm install

if [ $? -eq 0 ]; then
    echo "✅ Frontend dependencies installed successfully"
else
    echo "❌ Failed to install frontend dependencies"
    exit 1
fi

cd ..

echo "🎉 Setup completed successfully!"
echo ""
echo "To start the application:"
echo "  ./start.sh"
echo ""
echo "Or start components separately:"
echo "  App API:       cd backend && source venv/bin/activate && cd src/apis/app_api && python main.py"
echo "  Inference API: cd backend && source venv/bin/activate && cd src/apis/inference_api && python main.py"
echo "  Frontend:      cd frontend/ai.client && npm run start"
