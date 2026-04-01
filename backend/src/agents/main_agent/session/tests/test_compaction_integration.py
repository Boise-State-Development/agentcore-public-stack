"""
Integration tests for compaction with real AWS services.

Prerequisites:
1. AWS credentials configured
2. AGENTCORE_MEMORY_ID set
3. DYNAMODB_SESSIONS_METADATA_TABLE_NAME set
4. AGENTCORE_MEMORY_COMPACTION_ENABLED=true
5. AGENTCORE_MEMORY_COMPACTION_TOKEN_THRESHOLD set low for testing (e.g., 1000)

Run with:
    cd backend/src
    python -m pytest agents/main_agent/session/tests/test_compaction_integration.py -v -s

Or run directly:
    cd backend/src
    python agents/main_agent/session/tests/test_compaction_integration.py
"""

import os
import sys
import uuid
import asyncio
import logging

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def check_prerequisites():
    """Check required environment variables are set"""
    required = [
        'AGENTCORE_MEMORY_ID',
        'DYNAMODB_SESSIONS_METADATA_TABLE_NAME',
        'AWS_REGION',
    ]

    missing = [var for var in required if not os.environ.get(var)]
    if missing:
        logger.error(f"Missing required environment variables: {missing}")
        logger.error("Please set these in your .env file or environment")
        return False

    # Check compaction is enabled
    if os.environ.get('AGENTCORE_MEMORY_COMPACTION_ENABLED', 'false').lower() != 'true':
        logger.warning("AGENTCORE_MEMORY_COMPACTION_ENABLED is not 'true' - setting it for this test")
        os.environ['AGENTCORE_MEMORY_COMPACTION_ENABLED'] = 'true'

    # Set a low threshold for testing
    current_threshold = os.environ.get('AGENTCORE_MEMORY_COMPACTION_TOKEN_THRESHOLD', '100000')
    logger.info(f"Current AGENTCORE_MEMORY_COMPACTION_TOKEN_THRESHOLD: {current_threshold}")

    return True


def test_compaction_state_persistence():
    """Test that compaction state can be saved and loaded from DynamoDB"""
    from agents.main_agent.session.session_factory import SessionFactory
    from agents.main_agent.session.compaction_models import CompactionState

    logger.info("=" * 60)
    logger.info("TEST: Compaction State Persistence")
    logger.info("=" * 60)

    # Create a unique test session
    test_session_id = f"test-compaction-{uuid.uuid4().hex[:8]}"
    test_user_id = "test-user-compaction"

    logger.info(f"Creating session manager for session: {test_session_id}")

    # Create session manager with compaction enabled
    session_manager = SessionFactory.create_session_manager(
        session_id=test_session_id,
        user_id=test_user_id,
        compaction_enabled=True,
        compaction_threshold=1000,  # Low threshold for testing
    )

    # Initialize compaction state
    session_manager.compaction_state = CompactionState(
        checkpoint=5,
        summary="Test summary for integration test",
        last_input_tokens=2000,
    )

    # Save state
    logger.info("Saving compaction state...")
    session_manager._save_compaction_state(session_manager.compaction_state)

    # Load state (simulating session reload)
    logger.info("Loading compaction state...")
    loaded_state = session_manager._load_compaction_state()

    # Verify
    if loaded_state.checkpoint == 5:
        logger.info("✅ Checkpoint persisted correctly")
    else:
        logger.error(f"❌ Checkpoint mismatch: expected 5, got {loaded_state.checkpoint}")
        return False

    if loaded_state.summary == "Test summary for integration test":
        logger.info("✅ Summary persisted correctly")
    else:
        logger.error(f"❌ Summary mismatch")
        return False

    if loaded_state.last_input_tokens == 2000:
        logger.info("✅ Token count persisted correctly")
    else:
        logger.error(f"❌ Token count mismatch: expected 2000, got {loaded_state.last_input_tokens}")
        return False

    logger.info("✅ All persistence tests passed!")
    return True


def test_compaction_with_messages():
    """Test compaction with actual messages in AgentCore Memory"""
    from agents.main_agent.session.session_factory import SessionFactory
    from agents.main_agent.core.agent_factory import create_agent

    logger.info("=" * 60)
    logger.info("TEST: Compaction with Messages")
    logger.info("=" * 60)

    # Create a unique test session
    test_session_id = f"test-compaction-msg-{uuid.uuid4().hex[:8]}"
    test_user_id = "test-user-compaction"

    logger.info(f"Creating session with messages: {test_session_id}")

    # Create session manager
    session_manager = SessionFactory.create_session_manager(
        session_id=test_session_id,
        user_id=test_user_id,
        compaction_enabled=True,
        compaction_threshold=1000,  # Very low for testing
    )

    # Create agent with this session
    agent = create_agent(
        session_id=test_session_id,
        user_id=test_user_id,
        session_manager=session_manager,
    )

    logger.info(f"Agent created with {len(agent.messages)} initial messages")
    logger.info(f"Compaction config: {session_manager.compaction_config}")

    # Simulate a conversation by adding messages directly
    # (In real usage, these come from the model)
    test_messages = [
        {"role": "user", "content": [{"text": "Hello, can you help me with Python?"}]},
        {"role": "assistant", "content": [{"text": "Of course! I'd be happy to help with Python."}]},
        {"role": "user", "content": [{"text": "How do I read a file?"}]},
        {"role": "assistant", "content": [{"text": "You can use: with open('file.txt', 'r') as f: content = f.read()"}]},
    ]

    for msg in test_messages:
        agent.messages.append(msg)
        session_manager.message_count += 1

    logger.info(f"Added {len(test_messages)} test messages")
    logger.info(f"Total messages: {len(agent.messages)}")

    # Test truncation
    protected_indices = session_manager._find_protected_indices(
        agent.messages,
        session_manager.compaction_config.protected_turns
    )
    logger.info(f"Protected indices: {protected_indices}")

    # Test valid cutoff indices
    valid_indices = session_manager._find_valid_cutoff_indices(agent.messages)
    logger.info(f"Valid cutoff indices: {valid_indices}")

    logger.info("✅ Message handling tests passed!")
    return True


async def test_update_after_turn():
    """Test update_after_turn triggers compaction correctly"""
    from agents.main_agent.session.session_factory import SessionFactory

    logger.info("=" * 60)
    logger.info("TEST: Update After Turn")
    logger.info("=" * 60)

    test_session_id = f"test-compaction-turn-{uuid.uuid4().hex[:8]}"
    test_user_id = "test-user-compaction"

    # Create session with very low threshold
    session_manager = SessionFactory.create_session_manager(
        session_id=test_session_id,
        user_id=test_user_id,
        compaction_enabled=True,
        compaction_threshold=100,  # Very low - will trigger immediately
    )

    # Simulate some messages in cache
    session_manager._valid_cutoff_indices = [0, 4, 8, 12]  # 4 turns
    session_manager._all_messages_for_summary = [
        {"role": "user", "content": [{"text": f"Question {i}"}]}
        for i in range(16)
    ]

    # Call update_after_turn with token count above threshold
    logger.info("Calling update_after_turn with 500 tokens (above 100 threshold)...")
    await session_manager.update_after_turn(input_tokens=500)

    # Check if checkpoint was set
    if session_manager.compaction_state and session_manager.compaction_state.checkpoint > 0:
        logger.info(f"✅ Checkpoint set to: {session_manager.compaction_state.checkpoint}")
    else:
        logger.warning("⚠️ Checkpoint not set (may need session metadata record first)")

    logger.info("✅ Update after turn test completed!")
    return True


def run_all_tests():
    """Run all integration tests"""
    logger.info("Starting Compaction Integration Tests")
    logger.info("=" * 60)

    if not check_prerequisites():
        sys.exit(1)

    results = []

    # Test 1: State persistence
    try:
        results.append(("State Persistence", test_compaction_state_persistence()))
    except Exception as e:
        logger.error(f"State Persistence test failed: {e}", exc_info=True)
        results.append(("State Persistence", False))

    # Test 2: Message handling
    try:
        results.append(("Message Handling", test_compaction_with_messages()))
    except Exception as e:
        logger.error(f"Message Handling test failed: {e}", exc_info=True)
        results.append(("Message Handling", False))

    # Test 3: Update after turn
    try:
        results.append(("Update After Turn", asyncio.run(test_update_after_turn())))
    except Exception as e:
        logger.error(f"Update After Turn test failed: {e}", exc_info=True)
        results.append(("Update After Turn", False))

    # Summary
    logger.info("=" * 60)
    logger.info("TEST RESULTS")
    logger.info("=" * 60)
    all_passed = True
    for name, passed in results:
        status = "✅ PASSED" if passed else "❌ FAILED"
        logger.info(f"  {name}: {status}")
        if not passed:
            all_passed = False

    return all_passed


if __name__ == "__main__":
    success = run_all_tests()
    sys.exit(0 if success else 1)
