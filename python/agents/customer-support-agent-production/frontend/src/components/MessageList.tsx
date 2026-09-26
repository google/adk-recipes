import { useEffect, useRef } from 'react';
import { Bot, User } from 'lucide-react';
import type { Message } from '../types';

interface MessageListProps {
  messages: Message[];
  isLoading?: boolean;
}

export default function MessageList({ messages, isLoading }: MessageListProps) {
  const messagesEndRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages, isLoading]);

  return (
    <div style={styles.container}>
      {messages.length === 0 ? (
        <div style={styles.emptyState}>
          <Bot size={48} style={{ color: '#9ca3af' }} />
          <h2 style={styles.emptyTitle}>Welcome to Customer Support AI</h2>
          <p style={styles.emptyText}>
            Ask me about products, track your orders, or inquire about billing.
          </p>
          <div style={styles.examples}>
            <p style={styles.examplesTitle}>Try asking:</p>
            <ul style={styles.examplesList}>
              <li>"Search for gaming laptops"</li>
              <li>"Track order ORD-12345"</li>
              <li>"Show me invoice INV-2025-001"</li>
            </ul>
          </div>
        </div>
      ) : (
        <>
          {messages.map((message) => (
            <div
              key={message.id}
              style={{
                ...styles.messageWrapper,
                ...(message.role === 'user' ? styles.userWrapper : styles.assistantWrapper),
              }}
            >
              <div
                style={{
                  ...styles.message,
                  ...(message.role === 'user' ? styles.userMessage : styles.assistantMessage),
                }}
              >
                <div style={styles.messageIcon}>
                  {message.role === 'user' ? (
                    <User size={20} />
                  ) : (
                    <Bot size={20} />
                  )}
                </div>
                <div style={styles.messageContent}>
                  <div style={styles.messageText}>{message.content}</div>
                  <div style={styles.messageTime}>
                    {message.timestamp.toLocaleTimeString([], {
                      hour: '2-digit',
                      minute: '2-digit',
                    })}
                  </div>
                </div>
              </div>
            </div>
          ))}
          {isLoading && (
            <div style={{ ...styles.messageWrapper, ...styles.assistantWrapper }}>
              <div style={{ ...styles.message, ...styles.assistantMessage }}>
                <div style={styles.messageIcon}>
                  <Bot size={20} />
                </div>
                <div style={styles.messageContent}>
                  <div style={styles.loadingDots}>
                    <span></span>
                    <span></span>
                    <span></span>
                  </div>
                </div>
              </div>
            </div>
          )}
          <div ref={messagesEndRef} />
        </>
      )}
    </div>
  );
}

const styles: Record<string, React.CSSProperties> = {
  container: {
    flex: 1,
    overflowY: 'auto',
    padding: '24px',
    display: 'flex',
    flexDirection: 'column',
    gap: '16px',
  },
  emptyState: {
    display: 'flex',
    flexDirection: 'column',
    alignItems: 'center',
    justifyContent: 'center',
    height: '100%',
    textAlign: 'center',
    padding: '40px',
  },
  emptyTitle: {
    fontSize: '24px',
    fontWeight: '600',
    color: '#1f2937',
    marginTop: '16px',
    marginBottom: '8px',
  },
  emptyText: {
    fontSize: '16px',
    color: '#6b7280',
    marginBottom: '24px',
  },
  examples: {
    backgroundColor: '#f3f4f6',
    padding: '20px',
    borderRadius: '12px',
    textAlign: 'left',
    maxWidth: '400px',
  },
  examplesTitle: {
    fontSize: '14px',
    fontWeight: '600',
    color: '#374151',
    marginBottom: '12px',
  },
  examplesList: {
    listStyle: 'none',
    fontSize: '14px',
    color: '#6b7280',
    display: 'flex',
    flexDirection: 'column',
    gap: '8px',
  },
  messageWrapper: {
    display: 'flex',
    width: '100%',
  },
  userWrapper: {
    justifyContent: 'flex-end',
  },
  assistantWrapper: {
    justifyContent: 'flex-start',
  },
  message: {
    display: 'flex',
    gap: '12px',
    maxWidth: '70%',
    padding: '12px 16px',
    borderRadius: '16px',
  },
  userMessage: {
    backgroundColor: '#667eea',
    color: 'white',
    flexDirection: 'row-reverse',
  },
  assistantMessage: {
    backgroundColor: '#f3f4f6',
    color: '#1f2937',
  },
  messageIcon: {
    display: 'flex',
    alignItems: 'flex-start',
    paddingTop: '2px',
  },
  messageContent: {
    display: 'flex',
    flexDirection: 'column',
    gap: '4px',
  },
  messageText: {
    fontSize: '15px',
    lineHeight: '1.5',
    whiteSpace: 'pre-wrap',
    wordBreak: 'break-word',
  },
  messageTime: {
    fontSize: '12px',
    opacity: 0.7,
  },
  loadingDots: {
    display: 'flex',
    gap: '4px',
    padding: '4px 0',
  },
};
