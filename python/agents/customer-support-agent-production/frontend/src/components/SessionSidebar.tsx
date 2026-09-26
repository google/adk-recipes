import { useState, useEffect, useRef } from 'react';
import { sessionService } from '../services/api';
import { useToast } from './Toast';
import type { SessionInfo } from '../types';

interface SessionSidebarProps {
  currentSessionId: string | null;
  onSessionSelect: (sessionId: string) => void;
  onNewSession: () => void;
}

export default function SessionSidebar({
  currentSessionId,
  onSessionSelect,
  onNewSession,
}: SessionSidebarProps) {
  const [sessions, setSessions] = useState<SessionInfo[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState('');
  const [editingSessionId, setEditingSessionId] = useState<string | null>(null);
  const [editingName, setEditingName] = useState('');
  const previousSessionIdRef = useRef<string | null>(null);
  const toast = useToast();

  const loadSessions = async () => {
    try {
      setIsLoading(true);
      setError('');
      const response = await sessionService.listSessions();
      setSessions(response.sessions);
    } catch (err) {
      const errorMsg = err instanceof Error ? err.message : 'Failed to load sessions';
      setError(errorMsg);
      toast.error(errorMsg);
    } finally {
      setIsLoading(false);
    }
  };

  // Load sessions on mount
  useEffect(() => {
    loadSessions();
  }, []);

  // Reload sessions when a new session is created
  useEffect(() => {
    const prevSessionId = previousSessionIdRef.current;

    // Reload sessions when currentSessionId changes from null to a value (new session created)
    if (prevSessionId === null && currentSessionId !== null) {
      loadSessions();
    }

    previousSessionIdRef.current = currentSessionId;
  }, [currentSessionId]);

  const handleRename = async (sessionId: string) => {
    if (!editingName.trim()) {
      setEditingSessionId(null);
      return;
    }

    try {
      await sessionService.renameSession(sessionId, editingName);
      setSessions(sessions.map(s =>
        s.session_id === sessionId
          ? { ...s, session_name: editingName }
          : s
      ));
      setEditingSessionId(null);
      toast.success('Session renamed');
    } catch (err) {
      const errorMsg = err instanceof Error ? err.message : 'Failed to rename session';
      setError(errorMsg);
      toast.error(errorMsg);
    }
  };

  const handleDelete = async (sessionId: string) => {
    if (!confirm('Are you sure you want to delete this session?')) {
      return;
    }

    try {
      await sessionService.deleteSession(sessionId);
      setSessions(sessions.filter(s => s.session_id !== sessionId));

      if (currentSessionId === sessionId) {
        onNewSession();
      }
      toast.success('Session deleted');
    } catch (err) {
      const errorMsg = err instanceof Error ? err.message : 'Failed to delete session';
      setError(errorMsg);
      toast.error(errorMsg);
    }
  };

  const startEditing = (session: SessionInfo) => {
    setEditingSessionId(session.session_id);
    setEditingName(session.session_name);
  };

  const formatDate = (dateString: string) => {
    const date = new Date(dateString);
    const now = new Date();
    const diffMs = now.getTime() - date.getTime();
    const diffMins = Math.floor(diffMs / 60000);
    const diffHours = Math.floor(diffMs / 3600000);
    const diffDays = Math.floor(diffMs / 86400000);

    if (diffMins < 1) return 'Just now';
    if (diffMins < 60) return `${diffMins}m ago`;
    if (diffHours < 24) return `${diffHours}h ago`;
    if (diffDays < 7) return `${diffDays}d ago`;
    return date.toLocaleDateString();
  };

  return (
    <div className="session-sidebar">
      <div className="sidebar-header">
        <h2>Conversations</h2>
        <button
          className="new-session-btn"
          onClick={onNewSession}
          title="New conversation"
        >
          +
        </button>
      </div>

      {error && <div className="sidebar-error">{error}</div>}

      {isLoading ? (
        <div className="session-list">
          {[1, 2, 3].map((i) => (
            <div key={i} className="skeleton-session-item">
              <div className="skeleton-line skeleton-title" />
              <div className="skeleton-line skeleton-meta" />
            </div>
          ))}
        </div>
      ) : sessions.length === 0 ? (
        <div className="sidebar-empty">
          <p>No conversations yet</p>
          <p className="sidebar-empty-hint">Start a new chat to begin</p>
        </div>
      ) : (
        <div className="session-list">
          {sessions.map((session) => (
            <div
              key={session.session_id}
              className={`session-item ${
                currentSessionId === session.session_id ? 'active' : ''
              }`}
            >
              {editingSessionId === session.session_id ? (
                <div className="session-edit">
                  <input
                    type="text"
                    value={editingName}
                    onChange={(e) => setEditingName(e.target.value)}
                    onBlur={() => handleRename(session.session_id)}
                    onKeyDown={(e) => {
                      if (e.key === 'Enter') {
                        handleRename(session.session_id);
                      } else if (e.key === 'Escape') {
                        setEditingSessionId(null);
                      }
                    }}
                    autoFocus
                  />
                </div>
              ) : (
                <>
                  <div
                    className="session-info"
                    onClick={() => {
                      console.log('[SessionSidebar] Clicked session:', session.session_id);
                      onSessionSelect(session.session_id);
                    }}
                  >
                    <div className="session-name">{session.session_name}</div>
                    <div className="session-meta">
                      <span className="session-count">
                        {session.message_count} messages
                      </span>
                      <span className="session-date">
                        {formatDate(session.updated_at)}
                      </span>
                    </div>
                  </div>
                  <div className="session-actions">
                    <button
                      className="session-action-btn"
                      onClick={(e) => {
                        e.stopPropagation();
                        startEditing(session);
                      }}
                      title="Rename"
                    >
                      ✎
                    </button>
                    <button
                      className="session-action-btn delete"
                      onClick={(e) => {
                        e.stopPropagation();
                        handleDelete(session.session_id);
                      }}
                      title="Delete"
                    >
                      🗑
                    </button>
                  </div>
                </>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
