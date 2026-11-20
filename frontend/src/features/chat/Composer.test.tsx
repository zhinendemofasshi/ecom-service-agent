import { fireEvent, render, screen } from "@testing-library/react";
import { Composer } from "./Composer";

describe("Composer", () => {
  it("sends on Enter but not while an IME is composing", () => {
    const onSend = vi.fn();
    render(<Composer onSend={onSend} onStop={() => {}} busy={false} />);
    const box = screen.getByLabelText("消息");

    fireEvent.change(box, { target: { value: "退货" } });
    fireEvent.keyDown(box, { key: "Enter", isComposing: true });
    expect(onSend).not.toHaveBeenCalled();

    fireEvent.keyDown(box, { key: "Enter", shiftKey: true });
    expect(onSend).not.toHaveBeenCalled();

    fireEvent.keyDown(box, { key: "Enter" });
    expect(onSend).toHaveBeenCalledWith("退货");
    expect(box).toHaveValue("");
  });

  it("shows a stop button while the agent is busy", () => {
    const onStop = vi.fn();
    render(<Composer onSend={() => {}} onStop={onStop} busy />);
    fireEvent.click(screen.getByRole("button", { name: "停止生成" }));
    expect(onStop).toHaveBeenCalled();
  });
});
