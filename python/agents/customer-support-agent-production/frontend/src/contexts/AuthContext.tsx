import { createContext, useContext, useState, useEffect, ReactNode } from 'react';
import type { User, AuthResponse, AnonymousUserResponse } from '../types';

interface AuthContextType {
  user: User | null;
  token: string | null;
  isAuthenticated: boolean;
  isLoading: boolean;
  login: (email: string, password: string) => Promise<void>;
  register: (email: string, name: string, password: string) => Promise<void>;
  loginAsAnonymous: () => Promise<void>;
  logout: () => void;
}

const AuthContext = createContext<AuthContextType | undefined>(undefined);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [token, setToken] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(true);

  // Load user from localStorage on mount
  useEffect(() => {
    const storedUser = localStorage.getItem('user');
    const storedToken = localStorage.getItem('token');

    if (storedUser) {
      try {
        setUser(JSON.parse(storedUser));
        setToken(storedToken);
      } catch (error) {
        console.error('Failed to parse stored user:', error);
        localStorage.removeItem('user');
        localStorage.removeItem('token');
      }
    }

    setIsLoading(false);
  }, []);

  const login = async (email: string, password: string) => {
    const response = await fetch('/api/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email, password }),
    });

    if (!response.ok) {
      const error = await response.json();
      throw new Error(error.detail || 'Login failed');
    }

    const data: AuthResponse = await response.json();

    const user: User = {
      user_id: data.user_id,
      email: data.email,
      name: data.name,
      is_anonymous: false,
    };

    setUser(user);
    setToken(data.token);
    localStorage.setItem('user', JSON.stringify(user));
    localStorage.setItem('token', data.token);
  };

  const register = async (email: string, name: string, password: string) => {
    const response = await fetch('/api/auth/register', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email, name, password }),
    });

    if (!response.ok) {
      const error = await response.json();
      throw new Error(error.detail || 'Registration failed');
    }

    const data: AuthResponse = await response.json();

    const user: User = {
      user_id: data.user_id,
      email: data.email,
      name: data.name,
      is_anonymous: false,
    };

    setUser(user);
    setToken(data.token);
    localStorage.setItem('user', JSON.stringify(user));
    localStorage.setItem('token', data.token);
  };

  const loginAsAnonymous = async () => {
    const response = await fetch('/api/auth/anonymous', {
      method: 'POST',
    });

    if (!response.ok) {
      throw new Error('Failed to create anonymous user');
    }

    const data: AnonymousUserResponse = await response.json();

    const user: User = {
      user_id: data.user_id,
      is_anonymous: true,
    };

    setUser(user);
    setToken(null);
    localStorage.setItem('user', JSON.stringify(user));
    localStorage.removeItem('token');
  };

  const logout = () => {
    setUser(null);
    setToken(null);
    localStorage.removeItem('user');
    localStorage.removeItem('token');
    localStorage.removeItem('currentSessionId'); // Clear current session
  };

  const value: AuthContextType = {
    user,
    token,
    isAuthenticated: !!user && !user.is_anonymous,
    isLoading,
    login,
    register,
    loginAsAnonymous,
    logout,
  };

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth() {
  const context = useContext(AuthContext);
  if (context === undefined) {
    throw new Error('useAuth must be used within an AuthProvider');
  }
  return context;
}
