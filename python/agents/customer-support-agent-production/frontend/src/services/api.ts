import axios, { AxiosError } from 'axios';
import type {
  ChatResponse,
  ChatRequest,
  SessionListResponse,
  MessageHistoryResponse,
} from '../types';

// For development: http://localhost:8000
// For production (Cloud Run): uses relative URLs (served from same origin)
const API_BASE_URL = import.meta.env.VITE_API_URL || '';

// Retry configuration
const MAX_RETRIES = 3;
const RETRY_DELAY_MS = 1000;
const RETRYABLE_STATUS_CODES = [408, 429, 500, 502, 503, 504];

const apiClient = axios.create({
  baseURL: API_BASE_URL,
  headers: {
    'Content-Type': 'application/json',
  },
  timeout: 30000,
});

/**
 * Sleep for specified milliseconds
 */
function sleep(ms: number): Promise<void> {
  return new Promise(resolve => setTimeout(resolve, ms));
}

/**
 * Determine if an error is retryable
 */
function isRetryableError(error: AxiosError): boolean {
  // Network errors are retryable
  if (!error.response) {
    return true;
  }

  // Check if status code is retryable
  return RETRYABLE_STATUS_CODES.includes(error.response.status);
}

/**
 * Get user-friendly error message
 */
function getErrorMessage(error: AxiosError): string {
  if (!error.response) {
    return 'Network error. Please check your connection and try again.';
  }

  const status = error.response.status;
  const data = error.response.data as any;

  // Use server-provided message if available
  if (data?.detail) {
    return data.detail;
  }
  if (data?.error) {
    return data.error;
  }

  // Status-specific messages
  switch (status) {
    case 401:
      return 'Session expired. Please log in again.';
    case 403:
      return 'You don\'t have permission to perform this action.';
    case 404:
      return 'The requested resource was not found.';
    case 408:
    case 504:
      return 'Request timed out. Please try again.';
    case 429:
      return 'Too many requests. Please wait a moment and try again.';
    case 500:
    case 502:
    case 503:
      return 'Server error. Our team has been notified. Please try again later.';
    default:
      return 'An unexpected error occurred. Please try again.';
  }
}

/**
 * Execute request with retry logic
 */
async function withRetry<T>(
  requestFn: () => Promise<T>,
  maxRetries: number = MAX_RETRIES
): Promise<T> {
  let lastError: AxiosError | null = null;

  for (let attempt = 0; attempt <= maxRetries; attempt++) {
    try {
      return await requestFn();
    } catch (error) {
      if (!axios.isAxiosError(error)) {
        throw error;
      }

      lastError = error;

      // Don't retry if not retryable or last attempt
      if (!isRetryableError(error) || attempt === maxRetries) {
        break;
      }

      // Calculate delay with exponential backoff
      const delay = RETRY_DELAY_MS * Math.pow(2, attempt);
      console.log(`[API] Request failed, retrying in ${delay}ms (attempt ${attempt + 1}/${maxRetries})`);
      await sleep(delay);
    }
  }

  // Throw with user-friendly message
  throw new Error(getErrorMessage(lastError!));
}

// Helper to get auth headers
function getAuthHeaders(): Record<string, string> {
  const user = localStorage.getItem('user');
  const token = localStorage.getItem('token');

  if (!user) {
    throw new Error('No user found. Please login or create anonymous user.');
  }

  const userData = JSON.parse(user);
  const headers: Record<string, string> = {};

  if (token && !userData.is_anonymous) {
    // Authenticated user
    headers['Authorization'] = `Bearer ${token}`;
  } else {
    // Anonymous user
    headers['X-User-Id'] = userData.user_id;
  }

  return headers;
}

// =============================================================================
// CHAT SERVICE
// =============================================================================

export const chatService = {
  async sendMessage(message: string, sessionId?: string): Promise<ChatResponse> {
    const headers = getAuthHeaders();

    console.log('[API] Sending message:', {
      message: message.substring(0, 50),
      session_id: sessionId
    });

    const requestData: ChatRequest = {
      message,
      session_id: sessionId,
    };

    // Chat requests don't retry on server errors (to avoid duplicate messages)
    // Only retry on network/timeout errors
    const response = await withRetry(
      () => apiClient.post<ChatResponse>('/api/chat', requestData, {
        headers,
        timeout: 150000,  // 2.5 minutes - agent responses can take time
      }),
      1  // Only 1 retry for chat to avoid duplicates
    );

    console.log('[API] Received response:', {
      session_id: response.data.session_id,
      user_id: response.data.user_id
    });

    return response.data;
  },

  async healthCheck(): Promise<{ healthy: boolean; status?: string }> {
    try {
      const response = await apiClient.get('/health', { timeout: 5000 });
      const data = response.data;
      return {
        healthy: response.status === 200 && data.status !== 'unhealthy',
        status: data.status
      };
    } catch {
      return { healthy: false };
    }
  },
};

// =============================================================================
// SESSION SERVICE
// =============================================================================

export const sessionService = {
  async listSessions(): Promise<SessionListResponse> {
    const headers = getAuthHeaders();
    const response = await withRetry(
      () => apiClient.get<SessionListResponse>('/api/sessions', { headers })
    );
    return response.data;
  },

  async renameSession(sessionId: string, newName: string): Promise<void> {
    const headers = getAuthHeaders();
    await withRetry(
      () => apiClient.put(
        `/api/sessions/${sessionId}/rename`,
        { session_name: newName },
        { headers }
      )
    );
  },

  async deleteSession(sessionId: string): Promise<void> {
    const headers = getAuthHeaders();
    await withRetry(
      () => apiClient.delete(`/api/sessions/${sessionId}`, { headers })
    );
  },

  async getSessionMessages(sessionId: string): Promise<MessageHistoryResponse> {
    const headers = getAuthHeaders();
    const response = await withRetry(
      () => apiClient.get<MessageHistoryResponse>(
        `/api/sessions/${sessionId}/messages`,
        { headers }
      )
    );
    return response.data;
  },
};
