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

import { AlertTriangle, Check, ChevronRight, Trash2 } from "lucide-react";
import {
  type AnchorHTMLAttributes,
  type ButtonHTMLAttributes,
  forwardRef,
  type InputHTMLAttributes,
  type ReactNode,
  useState,
} from "react";

export type ButtonVariant = "primary" | "secondary" | "quiet" | "danger";
export type ButtonSize = "md" | "sm";

interface ButtonLookProps {
  variant?: ButtonVariant;
  size?: ButtonSize;
  icon?: ReactNode;
  iconFirst?: boolean;
}

function buttonClass(extra?: string): string {
  return ["ui-btn", extra].filter(Boolean).join(" ");
}

function buttonData({ variant = "secondary", size = "md" }: ButtonLookProps) {
  return {
    "data-variant": variant,
    "data-size": size,
  };
}

function Label({
  children,
  icon,
  iconFirst,
}: {
  children: ReactNode;
  icon?: ReactNode;
  iconFirst?: boolean;
}) {
  return (
    <>
      {iconFirst && icon}
      {children}
      {!iconFirst && icon}
    </>
  );
}

type ButtonProps = ButtonLookProps & ButtonHTMLAttributes<HTMLButtonElement>;

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(
  function Button(
    { variant, size, icon, iconFirst, className, children, ...rest },
    ref,
  ) {
    const look = { variant, size };
    return (
      <button
        ref={ref}
        type="button"
        className={buttonClass(className)}
        {...buttonData(look)}
        {...rest}
      >
        <Label icon={icon} iconFirst={iconFirst}>
          {children}
        </Label>
      </button>
    );
  },
);

type LinkButtonProps = ButtonLookProps &
  AnchorHTMLAttributes<HTMLAnchorElement>;

export function LinkButton({
  variant,
  size,
  icon,
  iconFirst,
  className,
  children,
  ...rest
}: LinkButtonProps) {
  const look = { variant, size };
  return (
    <a className={buttonClass(className)} {...buttonData(look)} {...rest}>
      <Label icon={icon} iconFirst={iconFirst}>
        {children}
      </Label>
    </a>
  );
}

export function IconButton({
  label,
  tone,
  className,
  children,
  ...rest
}: {
  label: string;
  tone?: "danger";
} & Omit<ButtonHTMLAttributes<HTMLButtonElement>, "aria-label">) {
  return (
    <button
      type="button"
      className={["ui-icon-btn", className].filter(Boolean).join(" ")}
      aria-label={label}
      title={rest.title ?? label}
      data-tone={tone}
      {...rest}
    >
      {children}
    </button>
  );
}

export function Switch({
  checked,
  onChange,
  label,
  disabled,
}: {
  checked: boolean;
  onChange: (checked: boolean) => void;
  label: string;
  disabled?: boolean;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      disabled={disabled}
      className="ui-switch"
      data-checked={checked || undefined}
      onClick={() => onChange(!checked)}
    >
      <span className="ui-switch-thumb" />
    </button>
  );
}

/** With `selected` set, renders a `role="radio"` picker option with a check mark instead of a chevron. */
export function Row({
  icon,
  title,
  subtitle,
  value,
  trailing,
  onClick,
  selected,
  testId,
  wrap = false,
}: {
  icon?: ReactNode;
  title: ReactNode;
  subtitle?: ReactNode;
  value?: ReactNode;
  trailing?: ReactNode;
  onClick?: () => void;
  selected?: boolean;
  testId?: string;
  wrap?: boolean;
}) {
  const isChoice = selected !== undefined;
  const body = (
    <>
      {icon && (
        <span className="ui-row-icon" aria-hidden>
          {icon}
        </span>
      )}
      <span className="ui-row-text">
        <span className="ui-row-title">{title}</span>
        {subtitle && <span className="ui-row-subtitle">{subtitle}</span>}
      </span>
      {value && <span className="ui-row-value">{value}</span>}
    </>
  );
  if (onClick) {
    return (
      <div
        className="ui-row"
        data-pressable
        data-wrap={wrap || undefined}
        data-testid={testId}
      >
        <button
          type="button"
          className="ui-row-main"
          onClick={onClick}
          {...(isChoice
            ? { role: "radio" as const, "aria-checked": selected }
            : {})}
        >
          {body}
          {isChoice
            ? selected && (
                <Check
                  size={16}
                  strokeWidth={2.5}
                  className="ui-row-check"
                  aria-hidden
                />
              )
            : !trailing && (
                <ChevronRight
                  size={16}
                  className="ui-row-chevron"
                  aria-hidden
                />
              )}
        </button>
        {trailing}
      </div>
    );
  }
  return (
    <div className="ui-row" data-wrap={wrap || undefined} data-testid={testId}>
      <span className="ui-row-main">{body}</span>
      {trailing}
    </div>
  );
}

export function InlineConfirm({
  trigger,
  question,
  confirmLabel = "Delete",
  onConfirm,
}: {
  trigger: (ask: () => void) => ReactNode;
  question?: ReactNode;
  confirmLabel?: string;
  onConfirm: () => void;
}) {
  const [asking, setAsking] = useState(false);
  if (!asking) return <>{trigger(() => setAsking(true))}</>;
  return (
    <span className="ui-confirm" role="alertdialog" aria-live="polite">
      {question && <span className="ui-confirm-question">{question}</span>}
      <span className="ui-confirm-actions">
        <Button size="sm" variant="secondary" onClick={() => setAsking(false)}>
          Keep
        </Button>
        <Button
          size="sm"
          variant="danger"
          onClick={() => {
            setAsking(false);
            onConfirm();
          }}
        >
          {confirmLabel}
        </Button>
      </span>
    </span>
  );
}

export function DeleteButton({
  onDelete,
  label,
}: {
  onDelete: () => void;
  label: string;
}) {
  return (
    <InlineConfirm
      onConfirm={onDelete}
      trigger={(ask) => (
        <IconButton label={label} tone="danger" onClick={ask}>
          <Trash2 size={14} aria-hidden />
        </IconButton>
      )}
    />
  );
}

export function EmptyState({
  title,
  children,
}: {
  title: string;
  children?: ReactNode;
}) {
  return (
    <div className="ui-empty">
      <p className="ui-empty-title">{title}</p>
      {children && <div className="ui-empty-body">{children}</div>}
    </div>
  );
}

export function ErrorNote({ children }: { children: ReactNode }) {
  return (
    <p className="ui-error" role="alert">
      <AlertTriangle size={14} aria-hidden />
      <span>{children}</span>
    </p>
  );
}

export function TextInput({
  className,
  ...rest
}: InputHTMLAttributes<HTMLInputElement>) {
  return (
    <input
      type="text"
      className={["ui-input", className].filter(Boolean).join(" ")}
      {...rest}
    />
  );
}
