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
    if [ ! -z "$APP_API_PID" ]; then
        echo "Stopping App API..."
        kill $APP_API_PID 2>/dev/null
        sleep 1
        kill -9 $APP_API_PID 2>/dev/null || true
    fi
    if [ ! -z "$INFERENCE_API_PID" ]; then
        echo "Stopping Inference API..."
        kill $INFERENCE_API_PID 2>/dev/null
        sleep 1
        kill -9 $INFERENCE_API_PID 2>/dev/null || true
    fi
    if [ ! -z "$FRONTEND_PID" ]; then
        echo "Stopping Frontend..."
        kill $FRONTEND_PID 2>/dev/null
        sleep 1
        kill -9 $FRONTEND_PID 2>/dev/null || true
    fi
    # Also clean up any remaining processes on ports
    lsof -ti:8000 2>/dev/null | xargs kill -9 2>/dev/null || true
    lsof -ti:8001 2>/dev/null | xargs kill -9 2>/dev/null || true
    lsof -ti:4200 2>/dev/null | xargs kill -9 2>/dev/null || true
    # Clean up log files
    if [ -f "app_api.log" ]; then
        rm app_api.log
    fi
    if [ -f "inference_api.log" ]; then
        rm inference_api.log
    fi
    exit 0
}

# Set up signal handlers
trap cleanup SIGINT SIGTERM

echo "Starting AgentCore Public Stack server..."

# Clean up any existing processes on ports
echo "Checking for existing processes on ports 8000, 8001, and 4200..."
if lsof -Pi :8000 -sTCP:LISTEN -t >/dev/null 2>&1 ; then
    echo "Killing process on port 8000..."
    lsof -ti:8000 | xargs kill -9 2>/dev/null || true
fi
if lsof -Pi :8001 -sTCP:LISTEN -t >/dev/null 2>&1 ; then
    echo "Killing process on port 8001..."
    lsof -ti:8001 | xargs kill -9 2>/dev/null || true
fi
if lsof -Pi :4200 -sTCP:LISTEN -t >/dev/null 2>&1 ; then
    echo "Killing process on port 4200..."
    lsof -ti:4200 | xargs kill -9 2>/dev/null || true
fi
# Wait for OS to release ports
if lsof -Pi :8000 -sTCP:LISTEN -t >/dev/null 2>&1 || lsof -Pi :8001 -sTCP:LISTEN -t >/dev/null 2>&1 || lsof -Pi :4200 -sTCP:LISTEN -t >/dev/null 2>&1 ; then
    echo "Waiting for ports to be released..."
    sleep 2
fi
echo "Ports cleared successfully"

# Get absolute path to project root and master .env file
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MASTER_ENV_FILE="$PROJECT_ROOT/backend/src/.env"

# Check if backend venv exists
if [ ! -d "backend/venv" ]; then
    echo "ERROR: Backend virtual environment not found. Please run setup first:"
    echo "  ./setup.sh"
    exit 1
fi

cd backend
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

# Start App API (port 8000)
echo "Starting App API on port 8000..."
cd "$PROJECT_ROOT/backend/src/apis/app_api"
env $(grep -v '^#' "$MASTER_ENV_FILE" 2>/dev/null | xargs) "$PROJECT_ROOT/backend/venv/bin/python" main.py > "$PROJECT_ROOT/app_api.log" 2>&1 &
APP_API_PID=$!

# Wait a moment before starting next service
sleep 2

# Start Inference API (port 8001)
echo "Starting Inference API on port 8001..."
cd "$PROJECT_ROOT/backend/src/apis/inference_api"
env $(grep -v '^#' "$MASTER_ENV_FILE" 2>/dev/null | xargs) "$PROJECT_ROOT/backend/venv/bin/python" main.py > "$PROJECT_ROOT/inference_api.log" 2>&1 &
INFERENCE_API_PID=$!

# Wait for both APIs to start
sleep 3

echo "App API is running on port: 8000"
echo "Inference API is running on port: 8001"

# Update environment variables for frontend
# Note: Configure which API the frontend should use
export API_URL="http://localhost:8001"

echo "Starting frontend server (local mode)..."
cd "$PROJECT_ROOT/frontend/ai.client"

unset PORT
NODE_NO_WARNINGS=1 npm run start &
FRONTEND_PID=$!

echo ""
echo "============================================"
echo "All services started successfully!"
echo "============================================"
echo ""
echo "Frontend:       http://localhost:4200"
echo "App API:        http://localhost:8000"
echo "  - API Docs:   http://localhost:8000/docs"
echo "Inference API:  http://localhost:8001"
echo "  - API Docs:   http://localhost:8001/docs"
echo ""
echo "Frontend is configured to use: $API_URL"
echo ""
echo "Logs:"
echo "  App API:       tail -f app_api.log"
echo "  Inference API: tail -f inference_api.log"
echo ""
echo "Press Ctrl+C to stop all services"
echo "============================================"

# Wait for background processes
wait
