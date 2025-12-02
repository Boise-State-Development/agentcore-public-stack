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
