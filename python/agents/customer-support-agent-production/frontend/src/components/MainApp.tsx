import { useState, useEffect, useRef } from 'react';
import { useAuth } from '../contexts/AuthContext';
import AuthScreen from './AuthScreen';
import SessionSidebar from './SessionSidebar';
import ChatInterface from './ChatInterface';

export default function MainApp() {
  const { user, isLoading, logout } = useAuth();
  const [currentSessionId, setCurrentSessionId] = useState<string | null>(null);
  const [showUserMenu, setShowUserMenu] = useState(false);
  const userMenuRef = useRef<HTMLDivElement>(null);

  // Close user menu when clicking outside
  useEffect(() => {
    const handleClickOutside = (event: MouseEvent) => {
      if (userMenuRef.current && !userMenuRef.current.contains(event.target as Node)) {
        setShowUserMenu(false);
      }
    };

    if (showUserMenu) {
      document.addEventListener('mousedown', handleClickOutside);
      return () => {
        document.removeEventListener('mousedown', handleClickOutside);
      };
    }
  }, [showUserMenu]);

  // Load current session from localStorage on mount
  useEffect(() => {
    if (user) {
      const savedSessionId = localStorage.getItem('currentSessionId');
      if (savedSessionId) {
        setCurrentSessionId(savedSessionId);
      }
    }
  }, [user]);

  // Save current session to localStorage
  useEffect(() => {
    console.log('[MainApp] currentSessionId changed to:', currentSessionId);
    if (currentSessionId) {
      localStorage.setItem('currentSessionId', currentSessionId);
    } else {
      localStorage.removeItem('currentSessionId');
    }
  }, [currentSessionId]);

  const handleSessionSelect = (sessionId: string) => {
    console.log('[MainApp] handleSessionSelect called with:', sessionId);
    console.log('[MainApp] Current session before update:', currentSessionId);
    setCurrentSessionId(sessionId);
  };

  const handleNewSession = () => {
    setCurrentSessionId(null);
  };

  const handleSessionCreated = (sessionId: string) => {
    setCurrentSessionId(sessionId);
  };

  const handleLogout = () => {
    setCurrentSessionId(null);
    logout();
  };

  if (isLoading) {
    return (
      <div className="app-loading">
        <div className="loading-spinner"></div>
        <p>Loading...</p>
      </div>
    );
  }

  if (!user) {
    return <AuthScreen />;
  }

  return (
    <div className="main-app">
      <SessionSidebar
        currentSessionId={currentSessionId}
        onSessionSelect={handleSessionSelect}
        onNewSession={handleNewSession}
      />

      <div className="chat-container">
        <div className="chat-header">
          <div className="chat-header-left">
            <h1>Customer Support Chat</h1>
          </div>
          <div className="chat-header-right">
            <div className="user-profile" ref={userMenuRef}>
              <button
                className="user-profile-btn"
                onClick={() => setShowUserMenu(!showUserMenu)}
              >
                <div className="user-avatar">
                  {user.is_anonymous ? '👤' : (user.name?.[0] || user.email?.[0] || 'U')}
                </div>
                <span className="user-name">
                  {user.is_anonymous ? 'Guest' : (user.name || user.email)}
                </span>
              </button>

              {showUserMenu && (
                <div className="user-menu">
                  <div className="user-menu-header">
                    {!user.is_anonymous && (
                      <>
                        <div className="user-menu-name">{user.name}</div>
                        <div className="user-menu-email">{user.email}</div>
                      </>
                    )}
                    {user.is_anonymous && (
                      <div className="user-menu-name">Guest User</div>
                    )}
                  </div>
                  <div className="user-menu-divider"></div>
                  <button
                    className="user-menu-item"
                    onClick={handleLogout}
                  >
                    Logout
                  </button>
                </div>
              )}
            </div>
          </div>
        </div>

        <div className="chat-content">
          <ChatInterface
            currentSessionId={currentSessionId}
            onSessionCreated={handleSessionCreated}
          />
        </div>
      </div>
    </div>
  );
}
