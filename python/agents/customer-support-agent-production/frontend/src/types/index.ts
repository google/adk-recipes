// =============================================================================
// USER & AUTHENTICATION
// =============================================================================

export interface User {
  user_id: string;
  email?: string;
  name?: string;
  is_anonymous: boolean;
}

export interface AuthResponse {
  user_id: string;
  token: string;
  name: string;
  email: string;
}

export interface LoginRequest {
  email: string;
  password: string;
}

export interface RegisterRequest {
  email: string;
  name: string;
  password: string;
}

export interface AnonymousUserResponse {
  user_id: string;
  is_anonymous: boolean;
}

// =============================================================================
// SESSIONS
// =============================================================================

export interface SessionInfo {
  session_id: string;
  session_name: string;
  created_at: string;
  updated_at: string;
  message_count: number;
  is_active: boolean;
}

export interface SessionListResponse {
  user_id: string;
  sessions: SessionInfo[];
}

// =============================================================================
// MESSAGES & CHAT
// =============================================================================

export interface Message {
  id: string;
  role: 'user' | 'assistant';
  content: string;
  timestamp: Date;
}

export interface ChatRequest {
  message: string;
  session_id?: string;
}

export interface ChatResponse {
  response: string;
  user_id: string;
  session_id: string;
}

export interface MessageInfo {
  message_id: string;
  session_id: string;
  role: 'user' | 'assistant';
  content: string;
  timestamp: string;
}

export interface MessageHistoryResponse {
  session_id: string;
  messages: MessageInfo[];
}

// =============================================================================
// ERRORS
// =============================================================================

export interface ApiError {
  error: string;
  details?: string;
}
