"""Shared pytest fixtures for agentic_coder tests."""
import pytest
from unittest.mock import MagicMock
import tempfile
import shutil
from pathlib import Path


@pytest.fixture
def mock_llm_client():
    """Mock LLM client for testing without external API calls."""
    client = MagicMock()
    client.complete.return_value = "Mocked LLM response"
    client.chat.return_value = "Mocked chat response"
    return client


@pytest.fixture
def sample_agent_state():
    """Sample agent state for testing."""
    return {
        "id": "agent-001",
        "task": "Test task",
        "status": "pending",
        "history": []
    }


@pytest.fixture
def sample_config():
    """Sample configuration for testing."""
    return {
        "model": "gpt-4",
        "temperature": 0.7,
        "max_tokens": 1000,
        "api_key": "test-key-placeholder"
    }


@pytest.fixture
def temp_workspace():
    """Temporary workspace directory that auto-cleans."""
    temp_dir = tempfile.mkdtemp()
    yield Path(temp_dir)
    shutil.rmtree(temp_dir, ignore_errors=True)
