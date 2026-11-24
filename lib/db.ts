import { neon, type NeonQueryFunction } from "@neondatabase/serverless";
import { databaseUrl } from "./config";

let client: NeonQueryFunction<false, false> | null = null;

/** Tagged-template SQL over Neon's HTTP driver (one statement per call). */
export function db(): NeonQueryFunction<false, false> {
  if (!client) client = neon(databaseUrl());
  return client;
}

/** Swap the SQL client, so tests can run the same queries against a local Postgres. */
export function setDbClientForTests(fn: NeonQueryFunction<false, false> | null): void {
  client = fn;
}

const SCHEMA: string[] = [
  `create table if not exists settings (
     key text primary key,
     value jsonb not null,
     updated_at timestamptz not null default now()
   )`,
  `create table if not exists items (
     id serial primary key,
     area text not null,
     kind text not null default 'task',
     title text not null,
     details text,
     status text not null default 'open',
     priority int not null default 2,
     due_date date,
     created_at timestamptz not null default now(),
     updated_at timestamptz not null default now(),
     completed_at timestamptz
   )`,
  `create index if not exists items_area_status on items (area, status)`,
  `create table if not exists reminders (
     id serial primary key,
     area text not null,
     text text not null,
     due_at timestamptz not null,
     recurrence text not null default 'none',
     status text not null default 'pending',
     last_sent_at timestamptz,
     created_at timestamptz not null default now(),
     updated_at timestamptz not null default now()
   )`,
  `create index if not exists reminders_status_due on reminders (status, due_at)`,
  `create table if not exists logs (
     id serial primary key,
     area text not null,
     metric text not null,
     value double precision,
     unit text,
     note text,
     logged_at timestamptz not null default now()
   )`,
  `create index if not exists logs_area_metric on logs (area, metric, logged_at desc)`,
  `create table if not exists messages (
     id serial primary key,
     direction text not null,
     source text not null,
     area text,
     text text not null,
     created_at timestamptz not null default now()
   )`,
  // Idempotency keys: forwarded agent replies, processed Telegram updates.
  `create table if not exists processed_keys (
     key text primary key,
     created_at timestamptz not null default now()
   )`,
];

export async function ensureSchema(): Promise<void> {
  const sql = db();
  for (const statement of SCHEMA) await sql.query(statement);
}

/** True when the error is Postgres "relation does not exist" (schema not created yet). */
export function isMissingTable(err: unknown): boolean {
  return typeof err === "object" && err !== null && (err as { code?: string }).code === "42P01";
}

/** Claim an idempotency key; returns false if it was already claimed. */
export async function claimKey(key: string): Promise<boolean> {
  const rows = await db()`insert into processed_keys (key) values (${key})
                          on conflict do nothing returning key`;
  return rows.length > 0;
}

export async function releaseKey(key: string): Promise<void> {
  await db()`delete from processed_keys where key = ${key}`;
}

export async function getSetting<T>(key: string): Promise<T | null> {
  const rows = await db()`select value from settings where key = ${key}`;
  return rows.length ? (rows[0].value as T) : null;
}

export async function setSetting(key: string, value: unknown): Promise<void> {
  await db()`insert into settings (key, value, updated_at)
             values (${key}, ${JSON.stringify(value)}::jsonb, now())
             on conflict (key) do update set value = excluded.value, updated_at = now()`;
}
