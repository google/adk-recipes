import { useState, FormEvent, KeyboardEvent, useEffect } from 'react';
import { Send, Mic, MicOff } from 'lucide-react';
import { useVoiceRecognition } from '../hooks/useVoiceRecognition';

interface MessageInputProps {
  onSendMessage: (message: string) => void;
  disabled?: boolean;
}

export default function MessageInput({ onSendMessage, disabled }: MessageInputProps) {
  const [input, setInput] = useState('');
  const {
    isListening,
    transcript,
    startListening,
    stopListening,
    resetTranscript,
    isSupported: isVoiceSupported,
    error: voiceError,
  } = useVoiceRecognition();

  // Update input when transcript changes (only when listening)
  useEffect(() => {
    if (isListening && transcript) {
      setInput(transcript);
    }
  }, [transcript, isListening]);

  const handleSubmit = (e: FormEvent) => {
    e.preventDefault();
    if (input.trim() && !disabled) {
      onSendMessage(input.trim());
      setInput('');
      resetTranscript();
    }
  };

  const toggleVoiceRecognition = () => {
    if (isListening) {
      stopListening();
    } else {
      resetTranscript();
      startListening();
    }
  };

  const handleKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSubmit(e);
    }
  };

  return (
    <form onSubmit={handleSubmit} style={styles.form}>
      {voiceError && isListening && (
        <div style={styles.voiceError}>
          {voiceError}
        </div>
      )}
      <div style={styles.inputContainer}>
        {isListening && (
          <div style={styles.listeningIndicator}>
            <div style={styles.pulsingDot} />
            <span style={styles.listeningText}>Listening...</span>
          </div>
        )}
        <textarea
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={handleKeyDown}
          placeholder={isListening ? "Speak now..." : "Type or click mic to speak..."}
          disabled={disabled}
          rows={1}
          style={{
            ...styles.textarea,
            ...(disabled ? styles.textareaDisabled : {}),
            ...(isListening ? styles.textareaListening : {}),
          }}
        />
        <div style={styles.buttonGroup}>
          {isVoiceSupported && (
            <button
              type="button"
              onClick={toggleVoiceRecognition}
              disabled={disabled}
              style={{
                ...styles.micButton,
                ...(isListening ? styles.micButtonActive : {}),
                ...(disabled ? styles.buttonDisabled : {}),
              }}
              title={isListening ? "Stop recording" : "Start voice input"}
            >
              {isListening ? <MicOff size={20} /> : <Mic size={20} />}
            </button>
          )}
          <button
            type="submit"
            disabled={disabled || !input.trim()}
            style={{
              ...styles.button,
              ...(disabled || !input.trim() ? styles.buttonDisabled : {}),
            }}
            title="Send message"
          >
            <Send size={20} />
          </button>
        </div>
      </div>
    </form>
  );
}

const styles: Record<string, React.CSSProperties> = {
  form: {
    display: 'flex',
    flexDirection: 'column',
    padding: '16px',
    borderTop: '1px solid #e5e7eb',
    backgroundColor: '#f9fafb',
  },
  inputContainer: {
    display: 'flex',
    flexDirection: 'column',
    gap: '8px',
    position: 'relative',
  },
  listeningIndicator: {
    display: 'flex',
    alignItems: 'center',
    gap: '8px',
    padding: '8px 12px',
    backgroundColor: '#fef3c7',
    borderRadius: '8px',
    fontSize: '14px',
    color: '#92400e',
  },
  pulsingDot: {
    width: '8px',
    height: '8px',
    borderRadius: '50%',
    backgroundColor: '#ef4444',
    animation: 'pulse 1.5s ease-in-out infinite',
  },
  listeningText: {
    fontWeight: '500',
  },
  textarea: {
    flex: 1,
    padding: '12px 16px',
    border: '1px solid #d1d5db',
    borderRadius: '12px',
    fontSize: '15px',
    resize: 'none',
    fontFamily: 'inherit',
    minHeight: '48px',
    maxHeight: '120px',
    outline: 'none',
    transition: 'border-color 0.2s, box-shadow 0.2s',
  },
  textareaListening: {
    borderColor: '#fbbf24',
    boxShadow: '0 0 0 3px rgba(251, 191, 36, 0.1)',
  },
  textareaDisabled: {
    backgroundColor: '#f3f4f6',
    cursor: 'not-allowed',
  },
  buttonGroup: {
    display: 'flex',
    gap: '8px',
    alignSelf: 'flex-end',
  },
  button: {
    padding: '12px 16px',
    backgroundColor: '#667eea',
    color: 'white',
    border: 'none',
    borderRadius: '12px',
    cursor: 'pointer',
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'center',
    transition: 'background-color 0.2s, transform 0.1s',
    minWidth: '48px',
  },
  micButton: {
    padding: '12px 16px',
    backgroundColor: '#10b981',
    color: 'white',
    border: 'none',
    borderRadius: '12px',
    cursor: 'pointer',
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'center',
    transition: 'background-color 0.2s, transform 0.1s',
    minWidth: '48px',
  },
  micButtonActive: {
    backgroundColor: '#ef4444',
    animation: 'pulse 1.5s ease-in-out infinite',
  },
  buttonDisabled: {
    backgroundColor: '#d1d5db',
    cursor: 'not-allowed',
  },
  voiceError: {
    padding: '8px 12px',
    backgroundColor: '#fee2e2',
    color: '#991b1b',
    fontSize: '13px',
    borderRadius: '8px',
    marginBottom: '8px',
  },
};
