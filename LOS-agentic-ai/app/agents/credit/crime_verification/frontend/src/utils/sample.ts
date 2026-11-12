// "Fill sample": a random, realistic (fictional) set of details from the config pools (crime_verification_demo.yaml
// `samples`, form.states). The PAN is never touched -- it stays the application's.
import type { Samples, UiConfig } from "../types/api";
import type { AddressParts } from "./address";

const pick = <T,>(list: T[]): T => list[Math.floor(Math.random() * list.length)];
const between = (lo: number, hi: number) => lo + Math.floor(Math.random() * (hi - lo + 1));

export interface SampleDetails {
  full_name: string;
  father_name: string;
  date_of_birth: string;
  address: AddressParts;
}

export function sampleDetails(samples: Samples, config: UiConfig, today = new Date()): SampleDetails {
  const last = pick(samples.last_names);
  const [young, old] = samples.age_years;
  const born = new Date(today.getFullYear() - between(young, old), between(0, 11), between(1, 28));
  const state = pick(Object.keys(config.form.states));
  const spec = config.form.states[state];
  const line = pick(samples.address_lines).replace("{n}", String(between(1, 450))).replace("{area}", pick(samples.areas));
  return {
    full_name: `${pick(samples.first_names)} ${last}`,
    father_name: `${pick(samples.father_first_names)} ${last}`,
    date_of_birth: `${born.getFullYear()}-${String(born.getMonth() + 1).padStart(2, "0")}-${String(born.getDate()).padStart(2, "0")}`,
    address: { line, state, city: pick(spec.cities), pin: `${pick(spec.pin_prefixes)}${String(between(0, 9999)).padStart(4, "0")}` },
  };
}

export function ageOf(dob: string, today = new Date()): number | null {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(dob);
  if (!m) return null;
  const y = Number(m[1]), mo = Number(m[2]) - 1, d = Number(m[3]);
  const before = today.getMonth() < mo || (today.getMonth() === mo && today.getDate() < d);
  const age = today.getFullYear() - y - (before ? 1 : 0);
  return age >= 0 && age < 150 ? age : null;
}
