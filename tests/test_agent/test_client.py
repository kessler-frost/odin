

from odin.agent.client import AgentEvent, OdinAgent


def test_odin_agent_instantiates():
    """OdinAgent can be created without starting."""
    agent = OdinAgent(tf_dir="tf")
    assert agent is not None
    assert not agent.is_running


def test_odin_agent_instantiates_with_tools():
    """OdinAgent accepts optional tools parameter."""
    agent = OdinAgent(tf_dir="tf", tools=None)
    assert agent is not None
    assert agent._tools is None


def test_agent_event_model():
    """AgentEvent holds event type and data."""
    event = AgentEvent(type="agent_message", data={"text": "Writing vpc..."})
    assert event.type == "agent_message"
    assert event.data["text"] == "Writing vpc..."


def test_load_session_id_missing(tmp_path, monkeypatch):
    """Returns None when no session file exists."""
    monkeypatch.setattr("odin.agent.client.SESSION_ID_FILE", tmp_path / "missing")
    agent = OdinAgent()
    assert agent._load_session_id() is None


def test_load_session_id_empty(tmp_path, monkeypatch):
    """Returns None when session file is empty."""
    session_file = tmp_path / "agent_session_id"
    session_file.write_text("")
    monkeypatch.setattr("odin.agent.client.SESSION_ID_FILE", session_file)
    agent = OdinAgent()
    assert agent._load_session_id() is None


def test_save_and_load_session_id(tmp_path, monkeypatch):
    """Session ID round-trips through save/load."""
    odin_dir = tmp_path / ".odin"
    session_file = odin_dir / "agent_session_id"
    monkeypatch.setattr("odin.agent.client.ODIN_DIR", odin_dir)
    monkeypatch.setattr("odin.agent.client.SESSION_ID_FILE", session_file)

    agent = OdinAgent()
    agent._save_session_id("test-session-abc123")

    assert agent._session_id == "test-session-abc123"
    assert session_file.exists()
    assert session_file.read_text() == "test-session-abc123"
    assert agent._load_session_id() == "test-session-abc123"


def test_save_session_creates_odin_dir(tmp_path, monkeypatch):
    """_save_session_id creates .odin directory if missing."""
    odin_dir = tmp_path / "nested" / ".odin"
    session_file = odin_dir / "agent_session_id"
    monkeypatch.setattr("odin.agent.client.ODIN_DIR", odin_dir)
    monkeypatch.setattr("odin.agent.client.SESSION_ID_FILE", session_file)

    agent = OdinAgent()
    agent._save_session_id("sess-42")

    assert odin_dir.exists()
    assert session_file.read_text() == "sess-42"
