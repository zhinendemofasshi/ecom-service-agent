import { useRef, useState } from "react";
import type { KeyboardEvent } from "react";
import { Button } from "@/components/ui";

export function Composer({
  onSend,
  onStop,
  busy,
  disabled,
  placeholder,
}: {
  onSend: (text: string) => void;
  onStop: () => void;
  busy: boolean;
  disabled?: boolean;
  placeholder?: string;
}) {
  const [text, setText] = useState("");
  const ref = useRef<HTMLTextAreaElement>(null);

  function submit() {
    const value = text.trim();
    if (!value || busy || disabled) return;
    onSend(value);
    setText("");
    ref.current?.focus();
  }

  function onKeyDown(e: KeyboardEvent<HTMLTextAreaElement>) {
    // Enter sends, Shift+Enter breaks a line. Ignore Enter while an IME
    // (pinyin etc.) is composing, otherwise picking a candidate sends the message.
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing && e.keyCode !== 229) {
      e.preventDefault();
      submit();
    }
  }

  return (
    <div className="flex items-end gap-2 rounded-2xl border border-line bg-surface p-2 shadow-sm focus-within:border-brand">
      <textarea
        ref={ref}
        rows={1}
        value={text}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={onKeyDown}
        disabled={disabled}
        placeholder={placeholder ?? "输入问题，Enter 发送，Shift+Enter 换行"}
        className="field-sizing-content max-h-40 min-h-10 flex-1 resize-none bg-transparent px-2 py-2 text-[15px] outline-none placeholder:text-muted disabled:cursor-not-allowed"
        aria-label="消息"
        maxLength={20000}
      />
      {busy ? (
        <Button variant="secondary" onClick={onStop} aria-label="停止生成">
          停止
        </Button>
      ) : (
        <Button onClick={submit} disabled={!text.trim() || disabled} aria-label="发送">
          发送
        </Button>
      )}
    </div>
  );
}
