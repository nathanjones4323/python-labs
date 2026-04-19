import { Text, View } from "react-native";

interface Props {
  children: string;
  className?: string;
  userMessage?: boolean;
}

type Segment = { text: string; bold?: boolean; italic?: boolean };

/** Parse inline **bold** and *italic* markers into segments. */
function parseInline(line: string): Segment[] {
  const segments: Segment[] = [];
  const re = /(\*\*(.+?)\*\*|\*(.+?)\*|_(.+?)_)/g;
  let last = 0;
  let match: RegExpExecArray | null;

  while ((match = re.exec(line)) !== null) {
    if (match.index > last) {
      segments.push({ text: line.slice(last, match.index) });
    }
    if (match[0].startsWith("**")) {
      segments.push({ text: match[2], bold: true });
    } else {
      segments.push({ text: match[3] ?? match[4], italic: true });
    }
    last = match.index + match[0].length;
  }
  if (last < line.length) {
    segments.push({ text: line.slice(last) });
  }
  return segments;
}

function InlineText({ segments, base }: { segments: Segment[]; base: string }) {
  return (
    <Text className={base}>
      {segments.map((seg, i) => (
        <Text
          key={i}
          className={[base, seg.bold ? "font-bold" : "", seg.italic ? "italic" : ""]
            .filter(Boolean)
            .join(" ")}
        >
          {seg.text}
        </Text>
      ))}
    </Text>
  );
}

export default function MarkdownText({ children, className = "", userMessage }: Props) {
  const base =
    className ||
    (userMessage
      ? "text-white text-sm leading-relaxed"
      : "text-gray-800 text-sm leading-relaxed");
  const lines = children.split("\n");

  const elements: React.ReactNode[] = [];
  let i = 0;

  while (i < lines.length) {
    const line = lines[i];

    // Blank line → small spacer
    if (line.trim() === "") {
      if (elements.length > 0) {
        elements.push(<View key={`gap-${i}`} className="h-2" />);
      }
      i++;
      continue;
    }

    // Horizontal rule: ---, ***, ___
    if (/^[-*_]{3,}$/.test(line.trim())) {
      elements.push(
        <View key={i} className="border-b border-gray-200 my-2" />
      );
      i++;
      continue;
    }

    // Table separator row: |---|---|---| — skip silently
    if (/^\|[-| :]+\|$/.test(line.trim())) {
      i++;
      continue;
    }

    // Table data row: | col | col | col |
    const tableRowMatch = line.trim().match(/^\|(.+)\|$/);
    if (tableRowMatch) {
      const cells = tableRowMatch[1].split("|").map((c) => c.trim());
      // Detect header row: check if next line is a separator
      const nextLine = lines[i + 1]?.trim() ?? "";
      const isHeader = /^\|[-| :]+\|$/.test(nextLine);
      elements.push(
        <View key={i} className={`flex-row ${isHeader ? "border-b border-gray-300 pb-1 mb-1" : "mb-0.5"}`}>
          {cells.map((cell, ci) => (
            <View key={ci} className="flex-1 pr-2">
              <InlineText
                segments={parseInline(cell)}
                base={isHeader ? `${base} font-semibold` : base}
              />
            </View>
          ))}
        </View>
      );
      i++;
      continue;
    }

    // Heading: # / ## / ###
    const headingMatch = line.match(/^(#{1,3})\s+(.+)$/);
    if (headingMatch) {
      const level = headingMatch[1].length;
      const headingBase =
        level === 1
          ? "text-base font-bold text-gray-900"
          : level === 2
          ? "text-sm font-bold text-gray-800"
          : "text-sm font-semibold text-gray-700";
      elements.push(
        <View key={i} className={`${level === 1 ? "mt-3 mb-1" : "mt-2 mb-0.5"}`}>
          <InlineText segments={parseInline(headingMatch[2])} base={headingBase} />
        </View>
      );
      i++;
      continue;
    }

    // Blockquote: > text
    const blockquoteMatch = line.match(/^>\s*(.*)$/);
    if (blockquoteMatch) {
      elements.push(
        <View key={i} className="border-l-2 border-gray-300 pl-3 my-1">
          <InlineText
            segments={parseInline(blockquoteMatch[1])}
            base={`${base} text-gray-600 italic`}
          />
        </View>
      );
      i++;
      continue;
    }

    // Numbered list item: "1. text" or "1) text"
    const numberedMatch = line.match(/^(\d+)[.)]\s+(.+)$/);
    if (numberedMatch) {
      elements.push(
        <View key={i} className="flex-row mb-1">
          <Text className={`${base} w-6 flex-shrink-0`}>{numberedMatch[1]}.</Text>
          <View className="flex-1">
            <InlineText segments={parseInline(numberedMatch[2])} base={base} />
          </View>
        </View>
      );
      i++;
      continue;
    }

    // Bullet list item: "- text", "* text", or "• text"
    const bulletMatch = line.match(/^[-*•]\s+(.+)$/);
    if (bulletMatch) {
      elements.push(
        <View key={i} className="flex-row mb-1">
          <Text className={`${base} w-4 flex-shrink-0`}>•</Text>
          <View className="flex-1">
            <InlineText segments={parseInline(bulletMatch[1])} base={base} />
          </View>
        </View>
      );
      i++;
      continue;
    }

    // Regular paragraph
    elements.push(<InlineText key={i} segments={parseInline(line)} base={base} />);
    i++;
  }

  return <View>{elements}</View>;
}
