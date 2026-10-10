// Server-Sent Events over a POST response. The browser's EventSource only does GET,
// so the chat stream is read with fetch and parsed here. No imports: the parser is
// tested on its own with Node's test runner (sse.test.ts).

export type SseMessage = { event: string; data: string };

/**
 * Splits a growing text buffer into complete events. Returns the events found and the
 * unfinished rest, to be prefixed to the next chunk. Handles \n and \r\n line endings,
 * several data lines per event (joined with \n) and comment lines (": keep-alive").
 */
export function parseSseChunk(buffer: string): { messages: SseMessage[]; rest: string } {
  const normalised = buffer.replace(/\r\n?/g, "\n");
  const blocks = normalised.split("\n\n");
  const rest = blocks.pop() ?? "";
  const messages: SseMessage[] = [];
  for (const block of blocks) {
    let event = "message";
    const data: string[] = [];
    for (const line of block.split("\n")) {
      if (!line || line.startsWith(":")) continue;
      const colon = line.indexOf(":");
      const field = colon === -1 ? line : line.slice(0, colon);
      let value = colon === -1 ? "" : line.slice(colon + 1);
      if (value.startsWith(" ")) value = value.slice(1);
      if (field === "event") event = value;
      else if (field === "data") data.push(value);
    }
    if (data.length) messages.push({ event, data: data.join("\n") });
  }
  return { messages, rest };
}

/** Reads a fetch Response body as events, calling onMessage for each one in order. */
export async function readSse(
  body: ReadableStream<Uint8Array>,
  onMessage: (message: SseMessage) => void,
): Promise<void> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const { messages, rest } = parseSseChunk(buffer);
    buffer = rest;
    messages.forEach(onMessage);
  }
  buffer += decoder.decode();
  const { messages } = parseSseChunk(buffer + "\n\n");
  messages.forEach(onMessage);
}
