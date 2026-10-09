// Copyright 2026 Google LLC
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     https://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
} from "@/components/ui/card";
import { ScrollArea } from "@/components/ui/scroll-area";
import {
  Loader2,
  Activity,
  Info,
  Search,
  TextSearch,
  Brain,
  Pen,
  ChevronDown,
  ChevronUp,
  Link,
} from "lucide-react";
import { useEffect, useState } from "react";
import ReactMarkdown from "react-markdown";

export interface ProcessedEvent {
  title: string;
  // biome-ignore lint/suspicious/noExplicitAny: event payload is dynamic
  data: any;
}

interface ActivityTimelineProps {
  processedEvents: ProcessedEvent[];
  isLoading: boolean;
  websiteCount: number;
}

export function ActivityTimeline({
  processedEvents,
  isLoading,
  websiteCount,
}: ActivityTimelineProps) {
  const [isTimelineCollapsed, setIsTimelineCollapsed] =
    useState<boolean>(false);

  // biome-ignore lint/suspicious/noExplicitAny: event payload is dynamic
  const formatEventData = (data: any): string => {
    // Handle new structured data types
    if (typeof data === "object" && data !== null && data.type) {
      switch (data.type) {
        case "functionCall":
          return `Calling function: ${data.name}\nArguments: ${JSON.stringify(data.args, null, 2)}`;
        case "functionResponse":
          return `Function ${data.name} response:\n${JSON.stringify(data.response, null, 2)}`;
        case "text":
          return data.content;
        case "sources": {
          const sources = data.content as Record<
            string,
            { title: string; url: string }
          >;
          if (Object.keys(sources).length === 0) {
            return "No sources found.";
          }
          return Object.values(sources)
            .map(
              (source) =>
                `[${source.title || "Untitled Source"}](${source.url})`,
            )
            .join(", ");
        }
        default:
          return JSON.stringify(data, null, 2);
      }
    }

    // Existing logic for backward compatibility
    if (typeof data === "string") {
      // Try to parse as JSON first
      try {
        const parsed = JSON.parse(data);
        return JSON.stringify(parsed, null, 2);
      } catch {
        // If not JSON, return as string (could be markdown)
        return data;
      }
    } else if (Array.isArray(data)) {
      return data.join(", ");
    } else if (typeof data === "object" && data !== null) {
      return JSON.stringify(data, null, 2);
    }
    return String(data);
  };

  // biome-ignore lint/suspicious/noExplicitAny: event payload is dynamic
  const isJsonData = (data: any): boolean => {
    // Handle new structured data types
    if (typeof data === "object" && data !== null && data.type) {
      if (data.type === "sources") {
        return false; // Let ReactMarkdown handle this
      }
      return data.type === "functionCall" || data.type === "functionResponse";
    }

    // Existing logic
    if (typeof data === "string") {
      try {
        JSON.parse(data);
        return true;
      } catch {
        return false;
      }
    }
    return typeof data === "object" && data !== null;
  };
  const getEventIcon = (title: string, index: number) => {
    if (index === 0 && isLoading && processedEvents.length === 0) {
      return <Loader2 className="h-4 w-4 text-neutral-400 animate-spin" />;
    }
    if (title.toLowerCase().includes("function call")) {
      return <Activity className="h-4 w-4 text-blue-400" />;
    } else if (title.toLowerCase().includes("function response")) {
      return <Activity className="h-4 w-4 text-green-400" />;
    } else if (title.toLowerCase().includes("generating")) {
      return <TextSearch className="h-4 w-4 text-neutral-400" />;
    } else if (title.toLowerCase().includes("thinking")) {
      return <Loader2 className="h-4 w-4 text-neutral-400 animate-spin" />;
    } else if (title.toLowerCase().includes("reflection")) {
      return <Brain className="h-4 w-4 text-neutral-400" />;
    } else if (title.toLowerCase().includes("research")) {
      return <Search className="h-4 w-4 text-neutral-400" />;
    } else if (title.toLowerCase().includes("finalizing")) {
      return <Pen className="h-4 w-4 text-neutral-400" />;
    } else if (title.toLowerCase().includes("retrieved sources")) {
      return <Link className="h-4 w-4 text-yellow-400" />;
    }
    return <Activity className="h-4 w-4 text-neutral-400" />;
  };

  useEffect(() => {
    if (!isLoading && processedEvents.length !== 0) {
      setIsTimelineCollapsed(true);
    }
  }, [isLoading, processedEvents]);
  return (
    <Card
      className={`border-none rounded-lg bg-neutral-700 ${isTimelineCollapsed ? "h-10 py-2" : "max-h-96 py-2"}`}
    >
      <CardHeader className="py-0">
        <CardDescription className="flex items-center justify-between">
          <button
            type="button"
            className="flex items-center justify-start text-sm w-full cursor-pointer gap-2 text-neutral-100 bg-transparent border-0 p-0 text-left"
            onClick={() => setIsTimelineCollapsed(!isTimelineCollapsed)}
          >
            <span>Research</span>
            {websiteCount > 0 && (
              <span className="text-xs bg-neutral-600 px-2 py-0.5 rounded-full">
                {websiteCount} websites
              </span>
            )}
            {isTimelineCollapsed ? (
              <ChevronDown className="h-4 w-4 mr-2" />
            ) : (
              <ChevronUp className="h-4 w-4 mr-2" />
            )}
          </button>
        </CardDescription>
      </CardHeader>
      {!isTimelineCollapsed && (
        <ScrollArea className="max-h-80 overflow-y-auto">
          <CardContent>
            {isLoading && processedEvents.length === 0 && (
              <div className="relative pl-8 pb-4">
                <div className="absolute left-3 top-3.5 h-full w-0.5 bg-neutral-800" />
                <div className="absolute left-0.5 top-2 h-5 w-5 rounded-full bg-neutral-800 flex items-center justify-center ring-4 ring-neutral-900">
                  <Loader2 className="h-3 w-3 text-neutral-400 animate-spin" />
                </div>
                <div>
                  <p className="text-sm text-neutral-300 font-medium">
                    Thinking...
                  </p>
                </div>
              </div>
            )}
            {processedEvents.length > 0 ? (
              <div className="space-y-0">
                {processedEvents.map((eventItem, index) => (
                  // biome-ignore lint/suspicious/noArrayIndexKey: events are append-only without unique IDs
                  <div key={index} className="relative pl-8 pb-4">
                    {index < processedEvents.length - 1 ||
                    (isLoading && index === processedEvents.length - 1) ? (
                      <div className="absolute left-3 top-3.5 h-full w-0.5 bg-neutral-600" />
                    ) : null}
                    <div className="absolute left-0.5 top-2 h-6 w-6 rounded-full bg-neutral-600 flex items-center justify-center ring-4 ring-neutral-700">
                      {getEventIcon(eventItem.title, index)}
                    </div>
                    <div>
                      <p className="text-sm text-neutral-200 font-medium mb-0.5">
                        {eventItem.title}
                      </p>
                      <div className="text-xs text-neutral-300 leading-relaxed">
                        {isJsonData(eventItem.data) ? (
                          <pre className="bg-neutral-800 p-2 rounded text-xs overflow-x-auto whitespace-pre-wrap">
                            {formatEventData(eventItem.data)}
                          </pre>
                        ) : (
                          <ReactMarkdown
                            components={{
                              p: ({ children }) => <span>{children}</span>,
                              a: ({ href, children }) => (
                                <a
                                  href={href}
                                  target="_blank"
                                  rel="noopener noreferrer"
                                  className="text-blue-400 hover:text-blue-300 underline"
                                >
                                  {children}
                                </a>
                              ),
                              code: ({ children }) => (
                                <code className="bg-neutral-800 px-1 py-0.5 rounded text-xs">
                                  {children}
                                </code>
                              ),
                            }}
                          >
                            {formatEventData(eventItem.data)}
                          </ReactMarkdown>
                        )}
                      </div>
                    </div>
                  </div>
                ))}
                {isLoading && processedEvents.length > 0 && (
                  <div className="relative pl-8 pb-4">
                    <div className="absolute left-0.5 top-2 h-5 w-5 rounded-full bg-neutral-600 flex items-center justify-center ring-4 ring-neutral-700">
                      <Loader2 className="h-3 w-3 text-neutral-400 animate-spin" />
                    </div>
                    <div>
                      <p className="text-sm text-neutral-300 font-medium">
                        Thinking...
                      </p>
                    </div>
                  </div>
                )}
              </div>
            ) : !isLoading ? ( // Only show "No activity" if not loading and no events
              <div className="flex flex-col items-center justify-center h-full text-neutral-500 pt-10">
                <Info className="h-6 w-6 mb-3" />
                <p className="text-sm">No activity to display.</p>
                <p className="text-xs text-neutral-600 mt-1">
                  Timeline will update during processing.
                </p>
              </div>
            ) : null}
          </CardContent>
        </ScrollArea>
      )}
    </Card>
  );
}
