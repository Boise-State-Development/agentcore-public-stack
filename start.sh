#!/bin/bash

echo "Starting AgentCore Public Stack..."

# Check if frontend dependencies are installed
if [ ! -d "frontend/ai.client/node_modules" ]; then
    echo "WARNING: Frontend dependencies not found. Please run setup first:"
    echo "  ./setup.sh"
    exit 1
fi

# Function to cleanup background processes
cleanup() {
    echo ""
    echo "Shutting down services..."
    if [ ! -z "$AGENTCORE_PID" ]; then
        kill $AGENTCORE_PID 2>/dev/null
        sleep 1
        # Force kill if still running
        kill -9 $AGENTCORE_PID 2>/dev/null || true
    fi
    if [ ! -z "$FRONTEND_PID" ]; then
        kill $FRONTEND_PID 2>/dev/null
        sleep 1
        kill -9 $FRONTEND_PID 2>/dev/null || true
    fi
    # Also clean up any remaining processes on ports
    lsof -ti:8000 2>/dev/null | xargs kill -9 2>/dev/null || true
    lsof -ti:4200 2>/dev/null | xargs kill -9 2>/dev/null || true
    # Clean up log file
    if [ -f "agentcore.log" ]; then
        rm agentcore.log
    fi
    exit 0
}

# Set up signal handlers
trap cleanup SIGINT SIGTERM

echo "Starting AgentCore Public Stack server..."

# Clean up any existing AgentCore and frontend processes
echo "Checking for existing processes on ports 8000 and 4200..."
if lsof -Pi :8000 -sTCP:LISTEN -t >/dev/null 2>&1 ; then
    echo "Killing process on port 8000..."
    lsof -ti:8000 | xargs kill -9 2>/dev/null || true
fi
if lsof -Pi :4200 -sTCP:LISTEN -t >/dev/null 2>&1 ; then
    echo "Killing process on port 4200..."
    lsof -ti:4200 | xargs kill -9 2>/dev/null || true
fi
# Wait for OS to release ports
if lsof -Pi :8000 -sTCP:LISTEN -t >/dev/null 2>&1 || lsof -Pi :4200 -sTCP:LISTEN -t >/dev/null 2>&1 ; then
    echo "Waiting for ports to be released..."
    sleep 2
fi
echo "Ports cleared successfully"

# Get absolute path to project root and master .env file
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MASTER_ENV_FILE="$PROJECT_ROOT/backend/src/.env"
CHATBOT_APP_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

cd backend/src
source venv/bin/activate

# Load environment variables from master .env file
if [ -f "$MASTER_ENV_FILE" ]; then
    echo "Loading environment variables from: $MASTER_ENV_FILE"
    set -a
    source "$MASTER_ENV_FILE"
    set +a
    echo "Environment variables loaded"
else
    echo "WARNING: Master .env file not found at $MASTER_ENV_FILE, using defaults"
    echo "Setting up local development defaults..."
fi

# Start AgentCore Runtime (port 8000)
cd src
env $(grep -v '^#' "$MASTER_ENV_FILE" 2>/dev/null | xargs) ../venv/bin/python main.py > "$CHATBOT_APP_ROOT/agentcore.log" 2>&1 &
AGENTCORE_PID=$!

# Wait for AgentCore to start
sleep 3

echo "AgentCore Runtime is running on port: 8000"

# Update environment variables for frontend
export API_URL="http://localhost:8000"

echo "Starting frontend server (local mode)..."
cd "$CHATBOT_APP_ROOT/frontend/ai.client"

unset PORT
NODE_NO_WARNINGS=1 npm run start &
FRONTEND_PID=$!

echo ""
echo "Services started successfully!"
echo ""
echo "Frontend: http://localhost:4200"
echo "API: http://localhost:8000"
echo "API Docs: http://localhost:8000/docs"
echo ""
echo "Frontend is configured to use API at: http://localhost:8000"
echo ""
echo "Press Ctrl+C to stop all services"

# Wait for background processes
wait
