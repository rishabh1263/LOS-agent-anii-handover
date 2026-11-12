import { ActionIcon, AppShell, Burger, Group, ScrollArea, Text, TextInput, ThemeIcon, UnstyledButton } from "@mantine/core";
import { useDisclosure } from "@mantine/hooks";
import { IconArrowRight, IconCheck, IconFileDescription, IconGavel, IconListCheck, IconListDetails, IconLock, IconScale,
         IconSearch, IconUserCircle } from "@tabler/icons-react";
import { useEffect, useState, type FormEvent } from "react";
import { Loader } from "./components/Loader";
import { Typewriter } from "./components/Typewriter";
import { CasePage } from "./pages/CasePage";
import { WorklistPage } from "./pages/WorklistPage";
import { ApiError, api } from "./services/api";
import type { Config, Workspace } from "./types";
import { initials, setScreen, setUi, t } from "./utils/text";

/** The case's steps, in order (owner 2026-10-30: four -- the summary lives in Final Status): the sidebar lists only
 *  these, under the Case ID box. */
export const STEPS = [
  { id: "application", icon: IconUserCircle }, { id: "report", icon: IconFileDescription }, { id: "rules", icon: IconListCheck },
  { id: "final", icon: IconGavel },
] as const;
export type StepId = (typeof STEPS)[number]["id"];
type Route = { page: "worklist" } | { page: "case"; id: string; step: StepId };

function parse(hash: string): Route {
  const [first, second, third] = hash.replace(/^#\/?/, "").split("/").filter(Boolean);
  if (first !== "cases" || !second) return { page: "worklist" };
  return { page: "case", id: decodeURIComponent(second), step: (STEPS.find((s) => s.id === third)?.id ?? "application") as StepId };
}

/** done / now / waiting for each step, from the case itself */
export function stepState(ws: Workspace | null, step: StepId): "done" | "open" | "waiting" {
  if (!ws) return "open";
  switch (step) {
    case "application": return ws.case.agency || ws.case.status === "WAIVED" ? "done" : "open";
    case "report": return ws.agency_report ? "done" : ws.case.agency ? "open" : "waiting";
    case "rules": return ws.run ? "done" : "waiting";
    default: return ["DECIDED", "WAIVED"].includes(ws.case.status) ? "done" : ws.run ? "open" : "waiting";
  }
}

export default function App() {
  const [route, setRoute] = useState<Route>(() => parse(window.location.hash));
  const [config, setConfig] = useState<Config | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [opened, { toggle, close }] = useDisclosure();
  const [lookup, setLookup] = useState(route.page === "case" ? route.id : "");
  const [ws, setWs] = useState<Workspace | null>(null);          // the open case, for the steps' states

  useEffect(() => {
    const onHash = () => { const r = parse(window.location.hash); setRoute(r); if (r.page === "case") setLookup(r.id); close(); };
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, [close]);
  useEffect(() => {
    api.config().then((c) => { setScreen(c.screen); setUi(c.ui); setConfig(c); })
      .catch((e) => setError(e instanceof ApiError ? e.message : "The service is not reachable."));
  }, []);
  useEffect(() => { window.scrollTo(0, 0); }, [route.page, route.page === "case" ? route.id + route.step : ""]);
  useEffect(() => { if (route.page !== "case") setWs(null); }, [route.page]);

  const go = (id: string, step: StepId = "application") => { window.location.hash = `#/cases/${encodeURIComponent(id)}/${step}`; };
  const find = (e: FormEvent) => { e.preventDefault(); const id = lookup.trim().toUpperCase(); if (id) go(id); };

  return (
    <AppShell header={{ height: { base: 56, md: 0 } }} navbar={{ width: 268, breakpoint: "md", collapsed: { mobile: !opened } }} padding={0}>
      <AppShell.Header className="top-header" hiddenFrom="md">
        <Group h="100%" px="md" gap="sm">
          <Burger opened={opened} onClick={toggle} size="sm" color="white" aria-label={config ? t("nav.label") : undefined} />
          <Text fw={800} c="white" size="sm">{config ? <Typewriter text={t("brand.title")} /> : ""}</Text>
        </Group>
      </AppShell.Header>

      <AppShell.Navbar className="side" p="md">
        <Group gap="sm" wrap="nowrap" mb="lg" visibleFrom="md">
          <ThemeIcon size={40} radius="md" className="brand-mark"><IconScale size={22} /></ThemeIcon>
          <div><Text fw={800} c="white" lh={1.15} className="brand-title">{config ? <Typewriter text={t("brand.title")} /> : ""}</Text>
            <Text size="xs" className="rail-dim" lh={1.2}>{config ? t("brand.subtitle") : ""}</Text></div>
        </Group>

        {config && (
          <form onSubmit={find} role="search" className="side-search">
            <Text size="xs" fw={700} tt="uppercase" lts={1} className="rail-dim" mb={6}>{t("case.case_id")}</Text>
            <TextInput value={lookup} onChange={(e) => setLookup(e.currentTarget.value)} placeholder="LGV-2026-001" radius="md"
                       aria-label={t("case.case_id")} leftSection={<IconSearch size={15} />} classNames={{ input: "side-input" }}
                       rightSection={<ActionIcon type="submit" radius="md" className="side-go" aria-label={t("case.open")}>
                         <IconArrowRight size={15} /></ActionIcon>} />
          </form>
        )}

        <AppShell.Section grow component={ScrollArea} mt="lg">
          {config && route.page === "case" && (
            <nav className="steps" aria-label={t("nav.case")}>
              {STEPS.map((s, i) => {
                const state = stepState(ws, s.id);
                const on = route.step === s.id;
                return (
                  <UnstyledButton key={s.id} className={`step step-${state}${on ? " is-on" : ""}`} onClick={() => go(route.id, s.id)}
                                  aria-current={on ? "step" : undefined}>
                    <span className="step-num">{state === "done" ? <IconCheck size={15} stroke={3} /> : state === "waiting" ? <IconLock size={13} /> : i + 1}</span>
                    <span className="step-text">
                      <Text fw={800} size="sm" lh={1.2}>{t(`sections.${s.id}`)}</Text>
                      <Text size="xs" lh={1.25} className="step-hint">{t(`steps_hint.${s.id}_${state}`)}</Text>
                    </span>
                    <s.icon size={17} className="step-icon" />
                  </UnstyledButton>
                );
              })}
            </nav>
          )}
        </AppShell.Section>

        {config && (
          <AppShell.Section>
            <UnstyledButton component="a" href="#/" className={`side-link${route.page === "worklist" ? " is-on" : ""}`}>
              <IconListDetails size={16} /> <Text size="sm" fw={700}>{t("nav.worklist")}</Text>
            </UnstyledButton>
            <Group gap="xs" mt="sm" className="side-user">
              <span className="side-avatar">{initials(config.user.name || config.user.id || "?")}</span>
              <Text size="xs" className="rail-dim" truncate>{config.user.name || config.user.id}</Text>
            </Group>
          </AppShell.Section>
        )}
      </AppShell.Navbar>

      <AppShell.Main className="main">
        {error && <div className="page"><Text c="red">{error}</Text></div>}
        {!config && !error && <Loader topic="start" />}
        {config && route.page === "worklist" && <WorklistPage onOpen={(id) => go(id)} />}
        {config && route.page === "case" && (
          <CasePage key={route.id} id={route.id} step={route.step} config={config} onBack={() => { window.location.hash = "#/"; }}
                    onStep={(s) => go(route.id, s)} onLoaded={setWs} />
        )}
      </AppShell.Main>
    </AppShell>
  );
}
