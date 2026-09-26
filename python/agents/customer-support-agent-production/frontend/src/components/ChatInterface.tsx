import { useState, useCallback, useEffect, useRef } from 'react';
import { MessageCircle, Volume2, VolumeX } from 'lucide-react';
import MessageList from './MessageList';
import MessageInput from './MessageInput';
import { chatService, sessionService } from '../services/api';
import { useTextToSpeech } from '../hooks/useTextToSpeech';
import { useToast } from './Toast';
import type { Message } from '../types';

interface ChatInterfaceProps {
  currentSessionId: string | null;
  onSessionCreated: (sessionId: string) => void;
}

export default function ChatInterface({ currentSessionId, onSessionCreated }: ChatInterfaceProps) {
  const [messages, setMessages] = useState<Message[]>([]);
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string>();
  const [autoSpeak, setAutoSpeak] = useState(false);
  const { speak, stop, isSpeaking, isSupported: isTTSSupported } = useTextToSpeech();
  const toast = useToast();
  const previousSessionIdRef = useRef<string | null>(null);
  const currentSessionIdRef = useRef<string | null>(currentSessionId);
  const [isLoadingHistory, setIsLoadingHistory] = useState(false);

  // Keep currentSessionIdRef in sync
  useEffect(() => {
    currentSessionIdRef.current = currentSessionId;
  }, [currentSessionId]);

  // Handle session switching: load messages from backend
  useEffect(() => {
    const prevSessionId = previousSessionIdRef.current;

    // Only process if session actually changed
    if (prevSessionId !== currentSessionId) {
      console.log('[ChatInterface] Session changed from', prevSessionId, 'to', currentSessionId);

      // Determine what to do with messages
      if (prevSessionId === null && currentSessionId !== null) {
        // New session being created - KEEP current messages
        console.log('[ChatInterface] New session created - keeping current messages');
        // Don't touch messages - they contain the conversation that just created this session
      } else if (currentSessionId !== null) {
        // Switching to an existing session - load from backend
        console.log('[ChatInterface] Loading session', currentSessionId, 'from backend');
        setIsLoadingHistory(true);
        sessionService.getSessionMessages(currentSessionId)
          .then((response) => {
            const loadedMessages: Message[] = response.messages.map(msg => ({
              id: msg.message_id,
              role: msg.role,
              content: msg.content,
              timestamp: new Date(msg.timestamp),
            }));
            console.log('[ChatInterface] Loaded', loadedMessages.length, 'messages from backend');
            setMessages(loadedMessages);
          })
          .catch((err) => {
            console.error('[ChatInterface] Failed to load messages:', err);
            toast.error('Failed to load conversation history');
            setMessages([]);
          })
          .finally(() => {
            setIsLoadingHistory(false);
          });
      } else {
        // New chat (click + button) - clear messages
        console.log('[ChatInterface] New chat - clearing messages');
        setMessages([]);
      }

      setError(undefined);
    }

    previousSessionIdRef.current = currentSessionId;
  }, [currentSessionId]);

  const handleSendMessage = useCallback(async (content: string) => {
    // Capture the session ID at request time to verify it hasn't changed when response arrives
    const requestSessionId = currentSessionId;

    const userMessage: Message = {
      id: Date.now().toString(),
      role: 'user',
      content,
      timestamp: new Date(),
    };

    setMessages((prev) => [...prev, userMessage]);
    setIsLoading(true);
    setError(undefined);

    console.log('[Frontend] Sending message with sessionId:', currentSessionId);

    try {
      const response = await chatService.sendMessage(content, currentSessionId || undefined);

      console.log('[Frontend] Received response with sessionId:', response.session_id);

      // If this was a new session, notify parent
      if (!currentSessionId && response.session_id) {
        onSessionCreated(response.session_id);
      }

      const assistantMessage: Message = {
        id: (Date.now() + 1).toString(),
        role: 'assistant',
        content: response.response,
        timestamp: new Date(),
      };

      // Determine if we should display the response
      // Use the ref to get the CURRENT session ID (not the stale closure value)
      const actualCurrentSessionId = currentSessionIdRef.current;
      const responseSessionId = response.session_id || requestSessionId;

      // Check if this is a new session creation and we're still in the new chat area
      const isNewSessionCreation = requestSessionId === null && response.session_id &&
                                   (actualCurrentSessionId === null || actualCurrentSessionId === responseSessionId);

      const isStillInSameSession = actualCurrentSessionId === responseSessionId;

      console.log('[ChatInterface] Response received - requestSessionId:', requestSessionId,
                  'responseSessionId:', responseSessionId,
                  'actualCurrentSessionId:', actualCurrentSessionId,
                  'isNewSessionCreation:', isNewSessionCreation,
                  'isStillInSameSession:', isStillInSameSession);

      if (isStillInSameSession || isNewSessionCreation) {
        // Display the response - either we're in the same session or creating a new one
        setMessages((prev) => [...prev, assistantMessage]);

        // Speak the response if auto-speak is enabled
        if (autoSpeak && isTTSSupported) {
          speak(response.response);
        }
      } else {
        // User switched to a different session while waiting
        // Messages are already saved to backend, so we don't need to do anything
        // When user switches back to that session, messages will be loaded from backend
        console.log('[ChatInterface] Session changed during request. Messages already saved to backend.');
      }
    } catch (err) {
      // Use the ref to get the CURRENT session ID (not the stale closure value)
      const actualCurrentSessionId = currentSessionIdRef.current;
      const isNewSessionCreation = requestSessionId === null &&
                                   (actualCurrentSessionId === null || actualCurrentSessionId === requestSessionId);
      const isStillInSameSession = actualCurrentSessionId === requestSessionId;

      console.log('[ChatInterface] Error occurred - requestSessionId:', requestSessionId,
                  'actualCurrentSessionId:', actualCurrentSessionId,
                  'isNewSessionCreation:', isNewSessionCreation,
                  'isStillInSameSession:', isStillInSameSession);

      // Only show error if we're still in the same session or creating a new one
      if (isStillInSameSession || isNewSessionCreation) {
        const errorMsg = err instanceof Error ? err.message : 'An error occurred';
        setError(errorMsg);

        // Show toast notification for the error
        toast.error(errorMsg);

        const errorMessage: Message = {
          id: (Date.now() + 1).toString(),
          role: 'assistant',
          content: errorMsg,
          timestamp: new Date(),
        };

        setMessages((prev) => [...prev, errorMessage]);
      }
    } finally {
      setIsLoading(false);
    }
  }, [currentSessionId, autoSpeak, isTTSSupported, speak, onSessionCreated, toast]);


  const toggleAutoSpeak = () => {
    if (autoSpeak && isSpeaking) {
      stop();
    }
    setAutoSpeak(!autoSpeak);
  };

  // Stop speaking when component unmounts
  useEffect(() => {
    return () => {
      stop();
    };
  }, [stop]);

  return (
    <div style={styles.container}>
      <div style={styles.header}>
        <div style={styles.headerContent}>
          <MessageCircle size={24} />
          <h1 style={styles.title}>Customer Support AI</h1>
        </div>
        <div style={styles.headerButtons}>
          {isTTSSupported && (
            <button
              onClick={toggleAutoSpeak}
              style={{
                ...styles.iconButton,
                ...(autoSpeak ? styles.iconButtonActive : {}),
              }}
              title={autoSpeak ? "Disable voice responses" : "Enable voice responses"}
            >
              {autoSpeak ? <Volume2 size={18} /> : <VolumeX size={18} />}
            </button>
          )}
        </div>
      </div>

      {error && (
        <div style={styles.errorBanner}>
          <span>{error}</span>
        </div>
      )}

      <MessageList messages={messages} isLoading={isLoading || isLoadingHistory} />
      <MessageInput onSendMessage={handleSendMessage} disabled={isLoading} />
    </div>
  );
}

const styles: Record<string, React.CSSProperties> = {
  container: {
    display: 'flex',
    flexDirection: 'column',
    height: '90vh',
    maxHeight: '800px',
    width: '100%',
    maxWidth: '900px',
    backgroundColor: 'white',
    borderRadius: '16px',
    boxShadow: '0 20px 25px -5px rgba(0, 0, 0, 0.1), 0 10px 10px -5px rgba(0, 0, 0, 0.04)',
    overflow: 'hidden',
  },
  header: {
    display: 'flex',
    justifyContent: 'space-between',
    alignItems: 'center',
    padding: '20px 24px',
    borderBottom: '1px solid #e5e7eb',
    backgroundColor: '#667eea',
    color: 'white',
  },
  headerContent: {
    display: 'flex',
    alignItems: 'center',
    gap: '12px',
  },
  title: {
    fontSize: '20px',
    fontWeight: '600',
    margin: 0,
  },
  headerButtons: {
    display: 'flex',
    alignItems: 'center',
    gap: '8px',
  },
  iconButton: {
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'center',
    padding: '8px',
    backgroundColor: 'rgba(255, 255, 255, 0.2)',
    color: 'white',
    border: 'none',
    borderRadius: '8px',
    cursor: 'pointer',
    transition: 'background-color 0.2s',
    minWidth: '36px',
    minHeight: '36px',
  },
  iconButtonActive: {
    backgroundColor: 'rgba(255, 255, 255, 0.3)',
  },
  resetButton: {
    display: 'flex',
    alignItems: 'center',
    gap: '8px',
    padding: '8px 16px',
    backgroundColor: 'rgba(255, 255, 255, 0.2)',
    color: 'white',
    border: 'none',
    borderRadius: '8px',
    cursor: 'pointer',
    fontSize: '14px',
    fontWeight: '500',
    transition: 'background-color 0.2s',
  },
  errorBanner: {
    padding: '12px 24px',
    backgroundColor: '#fee2e2',
    color: '#991b1b',
    fontSize: '14px',
    borderBottom: '1px solid #fecaca',
  },
};
