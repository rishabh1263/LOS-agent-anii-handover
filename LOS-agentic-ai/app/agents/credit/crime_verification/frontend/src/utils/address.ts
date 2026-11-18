// The permanent address as the form edits it (line + state + city + PIN) and as the server receives it: one string
// "<line>, <city>, <state> <PIN>". An application address that does not follow that shape stays whole in `line`.
import type { UiConfig } from "../types/api";

export interface AddressParts { line: string; city: string; state: string; pin: string }

export const emptyAddress: AddressParts = { line: "", city: "", state: "", pin: "" };

export function parseAddress(text: string, config: UiConfig): AddressParts {
  const value = (text ?? "").trim();
  const states = Object.keys(config.form.states);
  const m = /^(.*),\s*([^,]+),\s*([^,]+?)\s+(\d{6})$/.exec(value);
  if (m && states.includes(m[3].trim())) {
    return { line: m[1].trim(), city: m[2].trim(), state: m[3].trim(), pin: m[4] };
  }
  return { ...emptyAddress, line: value };
}

export function composeAddress(a: AddressParts): string {
  const tail = [a.state, a.pin].filter(Boolean).join(" ");
  return [a.line.trim(), a.city, tail].filter((x) => x && x.trim()).join(", ");
}

/** Cities of a state, with the parsed city kept even when the config does not list it. */
export function citiesOf(state: string, current: string, config: UiConfig): string[] {
  const list = config.form.states[state]?.cities ?? [];
  return current && !list.includes(current) ? [current, ...list] : list;
}
