import { useEffect, useState } from "react";
import type { JsonSchema } from "../api/types";
import { api } from "../api/client";
import type { Session } from "../api/types";

// Renders a plugin config JSON Schema as a form (backend/app/plugins/README.md §10.4).
// Supported: string / number / integer / boolean, enum (select), arrays of
// primitives (one item per line), one level of nested object, and the
// extensions x-secret, x-widget: "session" | "textarea". Anything else is
// edited as JSON. Validation is the backend's job; this only shapes input.

export const SECRET_MASK = "••••";

type Value = unknown;
type Obj = Record<string, unknown>;

const inputClass =
  "w-full rounded border border-gray-700 bg-gray-900 px-2 py-1 text-xs text-gray-200 outline-none focus:border-indigo-500";

const isObj = (v: unknown): v is Obj => !!v && typeof v === "object" && !Array.isArray(v);

function propsOf(schema: JsonSchema): [string, JsonSchema][] {
  const props = schema.properties;
  return isObj(props) ? Object.entries(props).filter((e): e is [string, JsonSchema] => isObj(e[1])) : [];
}

function typeOf(schema: JsonSchema): string | undefined {
  const t = schema.type;
  if (typeof t === "string") return t;
  // ["string", "null"] -> "string"
  if (Array.isArray(t)) return t.find((x) => x !== "null") as string | undefined;
  return undefined;
}

const PRIMITIVES = new Set(["string", "number", "integer", "boolean"]);

function isSupported(schema: JsonSchema, depth: number): boolean {
  if (Array.isArray(schema.enum)) return true;
  const t = typeOf(schema);
  if (t && PRIMITIVES.has(t)) return true;
  if (t === "array") {
    const items = isObj(schema.items) ? (schema.items as JsonSchema) : null;
    return !!items && (Array.isArray(items.enum) || PRIMITIVES.has(typeOf(items) ?? ""));
  }
  if (t === "object" && depth === 0) {
    return propsOf(schema).every(([, s]) => isSupported(s, depth + 1));
  }
  return false;
}

function label(key: string, schema: JsonSchema) {
  return typeof schema.title === "string" && schema.title ? schema.title : key;
}

function Description({ schema }: { schema: JsonSchema }) {
  return typeof schema.description === "string" && schema.description ? (
    <span className="text-[11px] text-gray-500">{schema.description}</span>
  ) : null;
}

// Sessions for x-widget: "session", fetched once per page load.
let sessionsPromise: Promise<Session[]> | null = null;
function useSessions(enabled: boolean): Session[] {
  const [sessions, setSessions] = useState<Session[]>([]);
  useEffect(() => {
    if (!enabled) return;
    sessionsPromise ??= api.sessions.list().catch(() => []);
    let alive = true;
    sessionsPromise.then((s) => alive && setSessions(s));
    return () => {
      alive = false;
    };
  }, [enabled]);
  return sessions;
}

function JsonField({ value, onChange }: { value: Value; onChange: (v: Value) => void }) {
  const [text, setText] = useState(() => JSON.stringify(value ?? null, null, 2));
  const [error, setError] = useState<string | null>(null);
  return (
    <div className="flex flex-col gap-1">
      <textarea
        className={`${inputClass} font-mono min-h-[60px]`}
        spellCheck={false}
        value={text}
        onChange={(e) => {
          setText(e.target.value);
          try {
            onChange(JSON.parse(e.target.value));
            setError(null);
          } catch (err) {
            setError((err as Error).message);
          }
        }}
      />
      {error && <span className="text-[11px] text-amber-400">Invalid JSON: {error}</span>}
    </div>
  );
}

function ArrayField({
  schema,
  value,
  onChange,
}: {
  schema: JsonSchema;
  value: Value;
  onChange: (v: Value) => void;
}) {
  const items = schema.items as JsonSchema;
  const itemType = typeOf(items);
  const list = Array.isArray(value) ? value : [];
  const [text, setText] = useState(() => list.map(String).join("\n"));
  return (
    <textarea
      className={`${inputClass} font-mono min-h-[48px]`}
      placeholder="One value per line"
      value={text}
      onChange={(e) => {
        setText(e.target.value);
        const lines = e.target.value.split("\n").map((l) => l.trim()).filter(Boolean);
        onChange(
          itemType === "number" || itemType === "integer"
            ? lines.map(Number)
            : itemType === "boolean"
              ? lines.map((l) => l === "true")
              : lines,
        );
      }}
    />
  );
}

function Field({
  name,
  schema,
  value,
  onChange,
  depth,
}: {
  name: string;
  schema: JsonSchema;
  value: Value;
  onChange: (v: Value) => void;
  depth: number;
}) {
  const type = typeOf(schema);
  const widget = schema["x-widget"];
  const sessions = useSessions(widget === "session");
  const title = label(name, schema);

  if (!isSupported(schema, depth)) {
    return (
      <div className="flex flex-col gap-1">
        <span className="text-xs text-gray-300">{title}</span>
        <JsonField value={value} onChange={onChange} />
        <Description schema={schema} />
      </div>
    );
  }

  if (type === "boolean" && !Array.isArray(schema.enum)) {
    return (
      <label className="flex items-start gap-2 text-xs text-gray-300">
        <input
          type="checkbox"
          className="mt-0.5"
          checked={value === true}
          onChange={(e) => onChange(e.target.checked)}
        />
        <span className="flex flex-col">
          {title}
          <Description schema={schema} />
        </span>
      </label>
    );
  }

  if (type === "object") {
    const obj = isObj(value) ? value : {};
    return (
      <fieldset className="flex flex-col gap-2 rounded border border-gray-800 p-2">
        <legend className="px-1 text-xs text-gray-400">{title}</legend>
        <Description schema={schema} />
        {propsOf(schema).map(([key, sub]) => (
          <Field
            key={key}
            name={key}
            schema={sub}
            value={obj[key]}
            depth={depth + 1}
            onChange={(v) => onChange({ ...obj, [key]: v })}
          />
        ))}
      </fieldset>
    );
  }

  let control;
  if (Array.isArray(schema.enum)) {
    const options = schema.enum as unknown[];
    const index = options.findIndex((o) => JSON.stringify(o) === JSON.stringify(value));
    control = (
      <select
        className={inputClass}
        value={index}
        onChange={(e) => onChange(Number(e.target.value) < 0 ? undefined : options[Number(e.target.value)])}
      >
        {index < 0 && <option value={-1}>—</option>}
        {options.map((o, i) => (
          <option key={i} value={i}>
            {String(o)}
          </option>
        ))}
      </select>
    );
  } else if (type === "array") {
    control = <ArrayField schema={schema} value={value} onChange={onChange} />;
  } else if (type === "number" || type === "integer") {
    control = (
      <input
        type="number"
        step={type === "integer" ? 1 : "any"}
        className={inputClass}
        value={typeof value === "number" ? value : ""}
        onChange={(e) => onChange(e.target.value === "" ? undefined : Number(e.target.value))}
      />
    );
  } else if (widget === "session") {
    control = (
      <select
        className={inputClass}
        value={typeof value === "string" ? value : ""}
        onChange={(e) => onChange(e.target.value || null)}
      >
        <option value="">—</option>
        {typeof value === "string" && value && !sessions.some((s) => s.id === value) && (
          <option value={value}>{value}</option>
        )}
        {sessions.map((s) => (
          <option key={s.id} value={s.id}>
            {s.title || s.id.slice(0, 8)}
          </option>
        ))}
      </select>
    );
  } else if (widget === "textarea") {
    control = (
      <textarea
        className={`${inputClass} min-h-[60px]`}
        value={typeof value === "string" ? value : ""}
        onChange={(e) => onChange(e.target.value)}
      />
    );
  } else {
    const secret = schema["x-secret"] === true;
    control = (
      <input
        type={secret ? "password" : "text"}
        className={inputClass}
        value={typeof value === "string" ? value : ""}
        // A masked secret is sent back unchanged to keep the stored value;
        // focusing it clears the field so the user types a fresh one.
        onFocus={(e) => secret && value === SECRET_MASK && e.currentTarget.select()}
        onChange={(e) => onChange(e.target.value)}
        autoComplete={secret ? "new-password" : undefined}
      />
    );
  }

  return (
    <label className="flex flex-col gap-1 text-xs text-gray-300">
      {title}
      {control}
      <Description schema={schema} />
    </label>
  );
}

export default function SchemaForm({
  schema,
  value,
  onChange,
}: {
  schema: JsonSchema;
  value: Obj;
  onChange: (v: Obj) => void;
}) {
  const props = propsOf(schema);
  if (props.length === 0) {
    return <span className="text-[11px] text-gray-500">No settings.</span>;
  }
  return (
    <div className="flex flex-col gap-2">
      {props.map(([key, sub]) => (
        <Field
          key={key}
          name={key}
          schema={sub}
          value={value[key]}
          depth={0}
          onChange={(v) => {
            const next = { ...value };
            if (v === undefined) delete next[key];
            else next[key] = v;
            onChange(next);
          }}
        />
      ))}
    </div>
  );
}
