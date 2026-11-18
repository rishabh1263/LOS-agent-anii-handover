import { Accordion, Alert, Badge, Button, Group, Paper, Radio, SegmentedControl, SimpleGrid, Stack, Table, Text, Textarea,
         ThemeIcon, Timeline } from "@mantine/core";
import { IconAlertTriangle, IconArrowRight, IconCheck, IconCircleCheck, IconGavel, IconListCheck, IconSparkles, IconX } from "@tabler/icons-react";
import { useEffect, useState } from "react";
import { api } from "../services/api";
import type { Config, Rule, Source, Summary, Workspace } from "../types";
import { t, when } from "../utils/text";
import { FieldGrid, recoColor, ruleColor, type Act } from "./kit";

const ICON = { FAIL: IconX, REVIEW: IconAlertTriangle, PASS: IconCheck } as const;
const SOURCES: Source[] = ["APPLICATION", "SALE_DEED", "CORRECTED", "AGENCY", "AGENT"];

// ---------------------------------------------------------------------------------------------- the assessment
const POLL_MS = 3000;
const POLL_LIMIT = 40;

/** ALWAYS AT THE TOP: the recommendation, its next action, and the summary in points (AI when it passed the check). */
export function Assessment({ ws, stacked = false, large = false }: { ws: Workspace; stacked?: boolean; large?: boolean }) {
  const ov = ws.run?.overview;
  const [s, setS] = useState<Summary | null>(null);
  const runId = ws.run?.run_id ?? null;
  useEffect(() => {
    if (!runId) { setS(null); return; }
    let alive = true;
    let tries = 0;
    let timer: number | undefined;
    const load = () => api.summary(ws.application.application_id).then((x) => {
      if (!alive) return;
      setS(x);
      if (x.pending && ++tries < POLL_LIMIT) timer = window.setTimeout(load, POLL_MS);
    }).catch(() => { /* the panel is a convenience */ });
    load();
    return () => { alive = false; window.clearTimeout(timer); };
  }, [runId, ws.application.application_id]);
  const tone = (p: string) => /^(critical|mismatch|agency vs)/i.test(p) ? "red" : /^(needs review|missing)/i.test(p) ? "yellow"
    : /^passed/i.test(p) ? "green" : "sapphire";

  return (
    <Paper p={0} className={`assessment${stacked ? " is-stacked" : ""}${large ? " is-large" : ""}`} withBorder>
      <div className={`assess-reco reco-${ov?.recommendation ?? "NONE"}`}>
        <Text size="xs" fw={800} tt="uppercase" lts={1} opacity={0.8}>{t("reco.title")}</Text>
        <Text fw={800} fz={22} lh={1.2}>{ov ? ov.recommendation_label : t("reco.not_run")}</Text>
        <Text size="xs" opacity={0.85} mt={4}>{ov ? ov.recommendation_note : t("reco.not_run_text")}</Text>
        {ov && (
          <Group gap={6} mt="md" wrap="nowrap" align="flex-start" className="next-action">
            <IconArrowRight size={16} style={{ flex: "none", marginTop: 2 }} />
            <Text size="sm" fw={700}>{ov.next_action_text}</Text>
          </Group>
        )}
      </div>
      <div className="assess-summary">
        <Group gap="xs" mb="xs">
          <ThemeIcon size={26} radius="md" variant="gradient">{s?.source === "ai" ? <IconSparkles size={15} /> : <IconListCheck size={15} />}</ThemeIcon>
          <Text fw={800} size="sm">{s?.label ?? t("summary.title")}</Text>
          {s?.pending && <Badge size="xs" color="cyan" variant="dot">{t("summary.writing")}</Badge>}
        </Group>
        {!runId && <Text size="sm" c="dimmed">{t("summary.none_text")}</Text>}
        {s?.points && (
          <ul className="points" style={{ listStyle: "none", paddingLeft: 0 }}>
            {s.points.map((p, i) => {
              const at = p.indexOf(":");
              return <li key={i} className={`p-${tone(p)}`}>{at > 0 && at < 28 ? <><b>{p.slice(0, at + 1)}</b>{p.slice(at + 1)}</> : p}</li>;
            })}
          </ul>
        )}
      </div>
    </Paper>
  );
}

// ---------------------------------------------------------------------------------------------- the ten rules
export function RuleResults({ ws, config }: { ws: Workspace; config: Config }) {
  const [filter, setFilter] = useState("ALL");
  const run = ws.run;
  if (!run) return <Text size="sm" c="dimmed">{ws.can.validate ? t("rules.not_run_text") : t("rules.not_run_wait")}</Text>;
  const rules = run.rules.filter((r) => r.id !== "R10");
  const n = (s: string) => rules.filter((r) => r.status === s).length;
  const shown = filter === "ALL" ? rules : rules.filter((r) => r.status === filter);
  return (
    <Stack gap="sm">
      <Group justify="space-between">
        <SegmentedControl size="xs" value={filter} onChange={setFilter} data={[
          { value: "ALL", label: `${t("rules.all")} ${rules.length}` },
          { value: "FAIL", label: `${t("rules.group.FAIL")} ${n("FAIL")}` },
          { value: "REVIEW", label: `${t("rules.group.REVIEW")} ${n("REVIEW")}` },
          { value: "PASS", label: `${t("rules.group.PASS")} ${n("PASS")}` }]} />
      </Group>
      <Accordion variant="separated" radius="md" chevronPosition="right" defaultValue={rules.find((r) => r.status === "FAIL")?.id}
                 className="rules">
        {shown.map((r) => <RuleItem key={r.id} rule={r} config={config} />)}
      </Accordion>
    </Stack>
  );
}

function RuleItem({ rule, config }: { rule: Rule; config: Config }) {
  const Icon = ICON[rule.status as keyof typeof ICON] ?? IconCheck;
  const used = SOURCES.filter((s) => rule.comparisons.some((c) => s in c.values));
  const notFound = (s: string) => <Text span size="sm" fs="italic" c="dimmed" className="value-missing">{t(`rules.missing_value.${s}`)}</Text>;
  return (
    <Accordion.Item value={rule.id} className={`rule rule-${rule.status}`}>
      <Accordion.Control icon={<ThemeIcon size={28} radius="md" variant="light" color={ruleColor(rule.status)}
                                          className={rule.status === "FAIL" ? "beat" : undefined}><Icon size={16} /></ThemeIcon>}>
        <Group justify="space-between" wrap="nowrap" gap="sm">
          <div style={{ minWidth: 0 }}>
            <Text fw={800} size="md" className="rule-title">{rule.name}</Text>
            <Text size="sm" c="dark.4" lineClamp={2}>{rule.reason}</Text>
            <Text size="xs" c="dimmed">{rule.explain}</Text>
          </div>
          <Group gap={6} wrap="nowrap">
            {rule.severity_label && <Badge size="xs" variant="filled" color={rule.severity === "CRITICAL" ? "red" : "orange"}>{rule.severity_label}</Badge>}
            <Badge color={ruleColor(rule.status)}>{rule.status_label}</Badge>
          </Group>
        </Group>
      </Accordion.Control>
      <Accordion.Panel>
        <Stack gap="sm">
          {rule.comparisons.length === 0 && (
            <Alert variant="light" color="gray" title={t("rules.nothing_compared")}>{rule.reason}</Alert>
          )}
          {rule.comparisons.length > 0 && (
            <Table withTableBorder withColumnBorders withRowBorders fz="sm" verticalSpacing={8} horizontalSpacing="md" className="cmp-table">
              <Table.Thead className="cmp-head"><Table.Tr><Table.Th>{t("rules.checked")}</Table.Th>
                {used.map((s) => <Table.Th key={s} className={`src-${s}`}>{config.sources[s] ?? s}</Table.Th>)}<Table.Th w={30} /></Table.Tr></Table.Thead>
              <Table.Tbody>
                {rule.comparisons.map((c, i) => (
                  <Table.Tr key={i}>
                    <Table.Td><Text fw={700} size="sm" c="dark.7">{c.label}</Text>{c.note && <Text size="xs" c="dimmed">{c.note}</Text>}</Table.Td>
                    {used.map((s) => <Table.Td key={s} fw={600}>{s in c.values ? c.values[s] ?? notFound(s) : null}</Table.Td>)}
                    <Table.Td>{c.match === true ? <IconCheck size={16} color="var(--mantine-color-green-7)" />
                      : c.match === false ? <IconX size={16} color="var(--mantine-color-red-7)" className="beat" />
                      : <IconAlertTriangle size={16} color="var(--mantine-color-yellow-7)" />}</Table.Td>
                  </Table.Tr>
                ))}
              </Table.Tbody>
            </Table>
          )}
          <SimpleGrid cols={{ base: 1, sm: 3 }} spacing="sm">
            {rule.why && <Alert variant="light" color="gray" title={t("rules.why")}>{rule.why}</Alert>}
            {rule.missing.length > 0 && <Alert variant="light" color="yellow" title={t("rules.missing")}>
              <ul className="tight">{rule.missing.map((m, i) => <li key={i}>{m}</li>)}</ul></Alert>}
            {rule.action_text && <Alert variant="light" color="sapphire" title={t("rules.action")}>{rule.action_text}</Alert>}
          </SimpleGrid>
        </Stack>
      </Accordion.Panel>
    </Accordion.Item>
  );
}

// ---------------------------------------------------------------------------------------------- final status
export function FinalStatus({ ws, config, act }: { ws: Workspace; config: Config; act: Act }) {
  const [choice, setChoice] = useState<string>("");
  const [remarks, setRemarks] = useState("");
  const [tried, setTried] = useState(false);
  const [all, setAll] = useState(false);
  const reco = ws.run?.overview.recommendation;
  const spec = config.decisions.find((d) => d.id === choice);
  const blocked = (d: string) => d === "PROCEED" && !!reco && !config.proceed_allowed_for.includes(reco);
  const needRemarks = !!spec?.remarks_required && !remarks.trim();
  const limit = config.ui.activity_visible ?? 6;
  const events = all ? ws.events : ws.events.slice(0, limit);
  const submit = async () => {
    setTried(true);
    if (!choice || needRemarks) return;
    await act("decide", () => api.decide(ws.application.application_id, choice, remarks), t("decision.recorded"));
  };

  return (
    <SimpleGrid cols={{ base: 1, lg: 2 }} spacing="lg">
      <Stack gap="sm">
        {ws.case.status === "DECIDED" ? (
          <Alert color="green" icon={<IconCircleCheck />} title={ws.case.decision_label}>
            {t("decision.by", { who: ws.case.decided_by ?? "—", when: when(ws.case.decided_at) })}
            {ws.case.decision_remarks && <Text size="sm" mt={4}>{ws.case.decision_remarks}</Text>}
          </Alert>
        ) : ws.case.status === "WAIVED" ? (
          <Alert color="orange" title={t("actions.waived_title")}>
            {t("actions.waived_by", { who: ws.case.waived_by ?? "—", when: when(ws.case.waived_at) })}
            <Text size="sm" mt={4}>{ws.case.waive_reason}</Text>
          </Alert>
        ) : !ws.can.decide ? (
          <Text size="sm" c="dimmed">{!ws.run ? t("decision.validate_first")
            : ws.case.status !== "VALIDATED" ? t("decision.rules_pending") : t("decision.no_permission")}</Text>
        ) : (
          <>
            <Radio.Group value={choice} onChange={setChoice} label={t("decision.choose")} error={tried && !choice ? t("decision.pick") : undefined}>
              <SimpleGrid cols={{ base: 1, sm: 2 }} spacing="xs" mt="xs">
                {config.decisions.map((d) => (
                  <Radio.Card key={d.id} value={d.id} disabled={blocked(d.id)} className="decision-card" radius="md" p="sm">
                    <Group wrap="nowrap" gap="sm" align="flex-start">
                      <Radio.Indicator disabled={blocked(d.id)} />
                      <div><Text size="sm" fw={700}>{d.label}</Text>
                        {d.remarks_required && <Text size="xs" c="dimmed">{t("decision.remarks_needed")}</Text>}
                        {blocked(d.id) && <Text size="xs" c="red">{t("decision.blocked")}</Text>}</div>
                    </Group>
                  </Radio.Card>
                ))}
              </SimpleGrid>
            </Radio.Group>
            <Textarea label={t("decision.remarks")} withAsterisk={!!spec?.remarks_required} value={remarks}
                      onChange={(e) => setRemarks(e.currentTarget.value)} placeholder={t("decision.remarks_hint")}
                      error={tried && needRemarks ? t("decision.remarks_required") : undefined} />
            <Group><Button leftSection={<IconGavel size={16} />} onClick={submit} variant="gradient" className="btn-anim">{t("decision.submit")}</Button></Group>
          </>
        )}
      </Stack>
      <Stack gap="xs">
        <Text size="xs" fw={800} tt="uppercase" c="dimmed">{t("audit.title")}</Text>
        {ws.events.length === 0 ? <Text size="sm" c="dimmed">{t("audit.none")}</Text> : (
          <Timeline active={events.length} bulletSize={14} lineWidth={2} color="sapphire">
            {events.map((e) => (
              <Timeline.Item key={e.event_id} title={<Text size="sm" fw={700}>{e.label}{e.detail && <Text span c="dimmed" fw={500}> · {e.detail}</Text>}</Text>}>
                <Text size="xs" c="dimmed">{e.actor} · {when(e.at)}</Text>
              </Timeline.Item>
            ))}
          </Timeline>
        )}
        {ws.events.length > limit && <Button variant="subtle" size="compact-xs" w="fit-content" onClick={() => setAll(!all)}>
          {all ? t("audit.less") : t("audit.all", { n: ws.events.length })}</Button>}
      </Stack>
    </SimpleGrid>
  );
}

// ---------------------------------------------------------------------------------------------- geo location
export function GeoLocation({ ws }: { ws: Workspace }) {
  const geo = ws.application.geo;
  if (!geo.some((g) => g.value)) return <Text size="sm" c="dimmed">{t("geo.none")}</Text>;
  return <FieldGrid items={geo.map((g) => ({ id: g.id, label: g.label, value: g.value, wide: g.id === "geo_address" }))}
                    cols={{ base: 1, sm: 2, lg: 4 }} />;
}

export { recoColor };
