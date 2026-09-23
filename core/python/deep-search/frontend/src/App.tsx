import { useCallback, useEffect, useRef, useState } from "react";
import { v4 as uuidv4 } from "uuid";
import { ChatMessagesView } from "@/components/ChatMessagesView";
import { WelcomeScreen } from "@/components/WelcomeScreen";

type DisplayData = string | null;

const BACKEND_HEALTH_CHECK_MAX_ATTEMPTS = 60;
const BACKEND_HEALTH_CHECK_INTERVAL_MS = 2000;

interface MessageWithAgent {
  type: "human" | "ai";
  content: string;
  id: string;
  agent?: string;
  finalReportWithCitations?: boolean;
}

interface ProcessedEvent {
  title: string;
  data: Record<string, unknown>;
}

interface StreamPart {
  text?: string;
  functionCall?: {
    name: string;
    args: Record<string, unknown>;
    id?: string;
  };
  functionResponse?: {
    name: string;
    response: Record<string, unknown>;
    id?: string;
  };
}

export default function App() {
  const [userId, setUserId] = useState<string | null>(null);
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [appName, setAppName] = useState<string | null>(null);
  const [messages, setMessages] = useState<MessageWithAgent[]>([]);
  const [displayData, setDisplayData] = useState<DisplayData>(null);
  const [isLoading, setIsLoading] = useState(false);
  const [messageEvents, setMessageEvents] = useState<
    Map<string, ProcessedEvent[]>
  >(new Map());
  const [websiteCount, setWebsiteCount] = useState<number>(0);
  const [isBackendReady, setIsBackendReady] = useState(false);
  const [isCheckingBackend, setIsCheckingBackend] = useState(true);
  const currentAgentRef = useRef("");
  const accumulatedTextRef = useRef("");
  const scrollAreaRef = useRef<HTMLDivElement>(null);

  const retryWithBackoff = useCallback(
    async <T,>(
      fn: () => Promise<T>,
      maxRetries = 10,
      maxDuration = 120000,
    ): Promise<T> => {
      const startTime = Date.now();
      let lastError: Error | undefined;

      for (let attempt = 0; attempt < maxRetries; attempt++) {
        if (Date.now() - startTime > maxDuration) {
          throw new Error(`Retry timeout after ${maxDuration}ms`);
        }

        try {
          return await fn();
        } catch (error) {
          lastError = error as Error;
          const delay = Math.min(1000 * 2 ** attempt, 5000);
          await new Promise((resolve) => setTimeout(resolve, delay));
        }
      }

      throw lastError ?? new Error("Retry failed");
    },
    [],
  );

  const createSession = useCallback(async (): Promise<{
    userId: string;
    sessionId: string;
    appName: string;
  }> => {
    const generatedSessionId = uuidv4();
    const response = await fetch(
      `/api/apps/app/users/u_999/sessions/${generatedSessionId}`,
      {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
      },
    );

    if (!response.ok) {
      throw new Error(
        `Failed to create session: ${response.status} ${response.statusText}`,
      );
    }

    const data = await response.json();
    return {
      userId: data.userId,
      sessionId: data.id,
      appName: data.appName,
    };
  }, []);

  const checkBackendHealth = useCallback(async (): Promise<boolean> => {
    try {
      const response = await fetch("/api/docs", {
        method: "GET",
        headers: {
          "Content-Type": "application/json",
        },
      });
      return response.ok;
    } catch {
      return false;
    }
  }, []);

  const extractDataFromSSE = useCallback((data: string) => {
    try {
      const parsed = JSON.parse(data);

      let textParts: string[] = [];
      let agent = "";
      let finalReportWithCitations: string | undefined;
      let functionCall: StreamPart["functionCall"];
      let functionResponse: StreamPart["functionResponse"];
      let sources: unknown = null;

      if (parsed.content && Array.isArray(parsed.content.parts)) {
        const parts = parsed.content.parts as StreamPart[];
        textParts = parts
          .filter((part): part is StreamPart & { text: string } =>
            Boolean(part.text),
          )
          .map((part) => part.text);

        const functionCallPart = parts.find((part) => part.functionCall);
        if (functionCallPart?.functionCall) {
          functionCall = functionCallPart.functionCall;
        }

        const functionResponsePart = parts.find(
          (part) => part.functionResponse,
        );
        if (functionResponsePart?.functionResponse) {
          functionResponse = functionResponsePart.functionResponse;
        }
      }

      if (parsed.author) {
        agent = parsed.author;
      }

      if (parsed.actions?.stateDelta?.final_report_with_citations) {
        finalReportWithCitations =
          parsed.actions.stateDelta.final_report_with_citations;
      }

      let sourceCount = 0;
      if (
        parsed.author === "section_researcher" ||
        parsed.author === "enhanced_search_executor"
      ) {
        if (parsed.actions?.stateDelta?.url_to_short_id) {
          sourceCount = Object.keys(
            parsed.actions.stateDelta.url_to_short_id,
          ).length;
        }
      }

      if (parsed.actions?.stateDelta?.sources) {
        sources = parsed.actions.stateDelta.sources;
      }

      return {
        textParts,
        agent,
        finalReportWithCitations,
        functionCall,
        functionResponse,
        sourceCount,
        sources,
      };
    } catch (error) {
      const truncatedData =
        data.length > 200 ? `${data.substring(0, 200)}...` : data;
      console.error(
        `Error parsing SSE data. Raw data (truncated): "${truncatedData}". Error details:`,
        error,
      );
      return {
        textParts: [],
        agent: "",
        finalReportWithCitations: undefined,
        functionCall: undefined,
        functionResponse: undefined,
        sourceCount: 0,
        sources: null,
      };
    }
  }, []);

  const getEventTitle = useCallback((agentName: string): string => {
    switch (agentName) {
      case "plan_generator":
        return "Planning Research Strategy";
      case "section_planner":
        return "Structuring Report Outline";
      case "section_researcher":
        return "Initial Web Research";
      case "research_evaluator":
        return "Evaluating Research Quality";
      case "EscalationChecker":
        return "Quality Assessment";
      case "enhanced_search_executor":
        return "Enhanced Web Research";
      case "research_pipeline":
        return "Executing Research Pipeline";
      case "iterative_refinement_loop":
        return "Refining Research";
      case "interactive_planner_agent":
      case "root_agent":
        return "Interactive Planning";
      default:
        return `Processing (${agentName || "Unknown Agent"})`;
    }
  }, []);

  const processSseEventData = useCallback(
    (jsonData: string, aiMessageId: string) => {
      const {
        textParts,
        agent,
        finalReportWithCitations,
        functionCall,
        functionResponse,
        sourceCount,
        sources,
      } = extractDataFromSSE(jsonData);

      if (sourceCount > 0) {
        setWebsiteCount((prev) => Math.max(prev, sourceCount));
      }

      if (agent && agent !== currentAgentRef.current) {
        currentAgentRef.current = agent;
      }

      if (functionCall) {
        const functionCallTitle = `Function Call: ${functionCall.name}`;
        setMessageEvents((prev) =>
          new Map(prev).set(aiMessageId, [
            ...(prev.get(aiMessageId) || []),
            {
              title: functionCallTitle,
              data: {
                type: "functionCall",
                name: functionCall.name,
                args: functionCall.args,
                id: functionCall.id,
              },
            },
          ]),
        );
      }

      if (functionResponse) {
        const functionResponseTitle = `Function Response: ${functionResponse.name}`;
        setMessageEvents((prev) =>
          new Map(prev).set(aiMessageId, [
            ...(prev.get(aiMessageId) || []),
            {
              title: functionResponseTitle,
              data: {
                type: "functionResponse",
                name: functionResponse.name,
                response: functionResponse.response,
                id: functionResponse.id,
              },
            },
          ]),
        );
      }

      if (textParts.length > 0 && agent !== "report_composer_with_citations") {
        if (agent !== "interactive_planner_agent") {
          const eventTitle = getEventTitle(agent);
          setMessageEvents((prev) =>
            new Map(prev).set(aiMessageId, [
              ...(prev.get(aiMessageId) || []),
              {
                title: eventTitle,
                data: { type: "text", content: textParts.join(" ") },
              },
            ]),
          );
        } else {
          for (const text of textParts) {
            accumulatedTextRef.current += `${text} `;
            setMessages((prev) =>
              prev.map((msg) =>
                msg.id === aiMessageId
                  ? {
                      ...msg,
                      content: accumulatedTextRef.current.trim(),
                      agent: currentAgentRef.current || msg.agent,
                    }
                  : msg,
              ),
            );
            setDisplayData(accumulatedTextRef.current.trim());
          }
        }
      }

      if (sources) {
        setMessageEvents((prev) =>
          new Map(prev).set(aiMessageId, [
            ...(prev.get(aiMessageId) || []),
            {
              title: "Retrieved Sources",
              data: { type: "sources", content: sources },
            },
          ]),
        );
      }

      if (
        agent === "report_composer_with_citations" &&
        finalReportWithCitations
      ) {
        const finalReportMessageId = `${Date.now().toString()}_final`;
        setMessages((prev) => [
          ...prev,
          {
            type: "ai",
            content: finalReportWithCitations,
            id: finalReportMessageId,
            agent: currentAgentRef.current,
            finalReportWithCitations: true,
          },
        ]);
        setDisplayData(finalReportWithCitations);
      }
    },
    [extractDataFromSSE, getEventTitle],
  );

  const handleSubmit = useCallback(
    async (query: string) => {
      if (!query.trim()) return;

      setIsLoading(true);
      try {
        let currentUserId = userId;
        let currentSessionId = sessionId;
        let currentAppName = appName;

        if (!currentSessionId || !currentUserId || !currentAppName) {
          const sessionData = await retryWithBackoff(createSession);
          currentUserId = sessionData.userId;
          currentSessionId = sessionData.sessionId;
          currentAppName = sessionData.appName;

          setUserId(currentUserId);
          setSessionId(currentSessionId);
          setAppName(currentAppName);
        }

        const userMessageId = Date.now().toString();
        setMessages((prev) => [
          ...prev,
          { type: "human", content: query, id: userMessageId },
        ]);

        const aiMessageId = `${Date.now().toString()}_ai`;
        currentAgentRef.current = "";
        accumulatedTextRef.current = "";

        setMessages((prev) => [
          ...prev,
          {
            type: "ai",
            content: "",
            id: aiMessageId,
            agent: "",
          },
        ]);

        const sendMessage = async () => {
          const response = await fetch("/api/run_sse", {
            method: "POST",
            headers: {
              "Content-Type": "application/json",
            },
            body: JSON.stringify({
              appName: currentAppName,
              userId: currentUserId,
              sessionId: currentSessionId,
              newMessage: {
                parts: [{ text: query }],
                role: "user",
              },
              streaming: false,
            }),
          });

          if (!response.ok) {
            throw new Error(
              `Failed to send message: ${response.status} ${response.statusText}`,
            );
          }

          return response;
        };

        const response = await retryWithBackoff(sendMessage);

        const reader = response.body?.getReader();
        const decoder = new TextDecoder();
        let lineBuffer = "";
        let eventDataBuffer = "";

        if (reader) {
          while (true) {
            const { done, value } = await reader.read();

            if (value) {
              lineBuffer += decoder.decode(value, { stream: true });
            }

            while (true) {
              const eolIndex = lineBuffer.indexOf("\n");
              if (eolIndex < 0 && !(done && lineBuffer.length > 0)) {
                break;
              }

              let line: string;
              if (eolIndex >= 0) {
                line = lineBuffer.substring(0, eolIndex);
                lineBuffer = lineBuffer.substring(eolIndex + 1);
              } else {
                line = lineBuffer;
                lineBuffer = "";
              }

              if (line.trim() === "") {
                if (eventDataBuffer.length > 0) {
                  const jsonDataToParse = eventDataBuffer.endsWith("\n")
                    ? eventDataBuffer.slice(0, -1)
                    : eventDataBuffer;
                  processSseEventData(jsonDataToParse, aiMessageId);
                  eventDataBuffer = "";
                }
              } else if (line.startsWith("data:")) {
                eventDataBuffer += `${line.substring(5).trimStart()}\n`;
              }
            }

            if (done) {
              if (eventDataBuffer.length > 0) {
                const jsonDataToParse = eventDataBuffer.endsWith("\n")
                  ? eventDataBuffer.slice(0, -1)
                  : eventDataBuffer;
                processSseEventData(jsonDataToParse, aiMessageId);
                eventDataBuffer = "";
              }
              break;
            }
          }
        }

        setIsLoading(false);
      } catch (error) {
        console.error("Error:", error);
        const aiMessageId = `${Date.now().toString()}_ai_error`;
        setMessages((prev) => [
          ...prev,
          {
            type: "ai",
            content: `Sorry, there was an error processing your request: ${error instanceof Error ? error.message : "Unknown error"}`,
            id: aiMessageId,
          },
        ]);
        setIsLoading(false);
      }
    },
    [
      appName,
      createSession,
      processSseEventData,
      retryWithBackoff,
      sessionId,
      userId,
    ],
  );

  // biome-ignore lint/correctness/useExhaustiveDependencies: scroll to bottom on new messages
  useEffect(() => {
    if (scrollAreaRef.current) {
      const scrollViewport = scrollAreaRef.current.querySelector(
        "[data-radix-scroll-area-viewport]",
      );
      if (scrollViewport) {
        scrollViewport.scrollTop = scrollViewport.scrollHeight;
      }
    }
  }, [messages]);

  useEffect(() => {
    const checkBackend = async () => {
      setIsCheckingBackend(true);

      let attempts = 0;

      while (attempts < BACKEND_HEALTH_CHECK_MAX_ATTEMPTS) {
        const isReady = await checkBackendHealth();
        if (isReady) {
          setIsBackendReady(true);
          setIsCheckingBackend(false);
          return;
        }

        attempts++;
        await new Promise((resolve) =>
          setTimeout(resolve, BACKEND_HEALTH_CHECK_INTERVAL_MS),
        );
      }

      setIsCheckingBackend(false);
      console.error("Backend failed to start within 2 minutes");
    };

    checkBackend();
  }, [checkBackendHealth]);

  const handleCancel = useCallback(() => {
    setMessages([]);
    setDisplayData(null);
    setMessageEvents(new Map());
    setWebsiteCount(0);
    window.location.reload();
  }, []);

  const BackendLoadingScreen = () => (
    <div className="flex-1 flex flex-col items-center justify-center p-4 overflow-hidden relative">
      <div
        className="w-full max-w-2xl z-10
                      bg-neutral-900/50 backdrop-blur-md 
                      p-8 rounded-2xl border border-neutral-700 
                      shadow-2xl shadow-black/60"
      >
        <div className="text-center space-y-6">
          <h1 className="text-4xl font-bold text-white flex items-center justify-center gap-3">
            ✨ Deep Search - ADK 🚀
          </h1>

          <div className="flex flex-col items-center space-y-4">
            <div className="relative">
              <div className="w-16 h-16 border-4 border-neutral-600 border-t-blue-500 rounded-full animate-spin"></div>
              <div
                className="absolute inset-0 w-16 h-16 border-4 border-transparent border-r-purple-500 rounded-full animate-spin"
                style={{
                  animationDirection: "reverse",
                  animationDuration: "1.5s",
                }}
              ></div>
            </div>

            <div className="space-y-2">
              <p className="text-xl text-neutral-300">
                Waiting for backend to be ready...
              </p>
              <p className="text-sm text-neutral-400">
                This may take a moment on first startup
              </p>
            </div>

            <div className="flex space-x-1">
              <div
                className="w-2 h-2 bg-blue-500 rounded-full animate-bounce"
                style={{ animationDelay: "0ms" }}
              ></div>
              <div
                className="w-2 h-2 bg-purple-500 rounded-full animate-bounce"
                style={{ animationDelay: "150ms" }}
              ></div>
              <div
                className="w-2 h-2 bg-pink-500 rounded-full animate-bounce"
                style={{ animationDelay: "300ms" }}
              ></div>
            </div>
          </div>
        </div>
      </div>
    </div>
  );

  return (
    <div className="flex h-screen bg-neutral-800 text-neutral-100 font-sans antialiased">
      <main className="flex-1 flex flex-col overflow-hidden w-full">
        <div
          className={`flex-1 overflow-y-auto ${messages.length === 0 || isCheckingBackend ? "flex" : ""}`}
        >
          {isCheckingBackend ? (
            <BackendLoadingScreen />
          ) : !isBackendReady ? (
            <div className="flex-1 flex flex-col items-center justify-center p-4">
              <div className="text-center space-y-4">
                <h2 className="text-2xl font-bold text-red-400">
                  Backend Unavailable
                </h2>
                <p className="text-neutral-300">
                  Unable to connect to backend services at localhost:8000
                </p>
                <button
                  type="button"
                  onClick={() => window.location.reload()}
                  className="px-4 py-2 bg-blue-600 hover:bg-blue-700 rounded-lg transition-colors"
                >
                  Retry
                </button>
              </div>
            </div>
          ) : messages.length === 0 ? (
            <WelcomeScreen
              handleSubmit={handleSubmit}
              isLoading={isLoading}
              onCancel={handleCancel}
            />
          ) : (
            <ChatMessagesView
              messages={messages}
              isLoading={isLoading}
              scrollAreaRef={scrollAreaRef}
              onSubmit={handleSubmit}
              onCancel={handleCancel}
              displayData={displayData}
              messageEvents={messageEvents}
              websiteCount={websiteCount}
            />
          )}
        </div>
      </main>
    </div>
  );
}
