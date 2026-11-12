import { Avatar, Badge, Button, Group, Menu, Paper, Stack, Text, ThemeIcon } from "@mantine/core";
import { IconArrowLeft, IconChevronDown, IconDownload, IconLock, IconRefresh } from "@tabler/icons-react";
import { useCallback, useEffect, useState } from "react";
import { type StepId } from "../App";
import { ApplicationStep } from "../components/ApplicationStep";
import { notifyError, notifyOk, recoColor, statusColor, type Act } from "../components/kit";
import { Loader } from "../components/Loader";
import { Assessment, FinalStatus, RuleResults } from "../components/Outcome";
import { VerificationReport } from "../components/VerificationReport";
import { ApiError, api, saveBlob } from "../services/api";
import type { Config, Workspace } from "../types";
import { initials, rupees, t } from "../utils/text";

/** the actions that cover the page with the loader while they run, and the loader's lines for each */
const LOADER_FOR: Record<string, string> = { upload: "upload", validate: "validate", report: "submit_report", generate: "generate_report", send: "send",
                                              download: "report", decide: "decide", deed: "deed", save: "save", waive: "waive" };

/** One case: a slim strip of its key facts, then the step the sidebar chose at full width (a short spin of the loader
 *  while switching); the recommendation and the summary in points are the last step. */
export function CasePage({ id, step, config, onBack, onStep, onLoaded }: {
  id: string; step: StepId; config: Config; onBack: () => void; onStep: (s: StepId) => void; onLoaded: (ws: Workspace | null) => void;
}) {
  const [ws, setWs] = useState<Workspace | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [shown, setShown] = useState(step);
  const [switching, setSwitching] = useState(false);

  const load = useCallback(async () => {
    try { const w = await api.workspace(id); setWs(w); setError(null); }
    catch (e) { setError(e instanceof ApiError ? e.message : t("common.failed")); }
  }, [id]);
  useEffect(() => { load(); }, [load]);
  useEffect(() => { onLoaded(ws); }, [ws, onLoaded]);
  useEffect(() => {
    if (step === shown) return;
    setSwitching(true);
    const timer = window.setTimeout(() => { setShown(step); setSwitching(false); }, config.ui.switch_ms ?? 500);
    return () => window.clearTimeout(timer);
  }, [step, shown, config.ui.switch_ms]);

  const act: Act = async (key, run, ok) => {
    setBusy(key);
    try {
      const next = await run();
      if (next) setWs(next);
      if (ok) notifyOk(ok);
      if (key === "send" && next?.agency_report) onStep("report");     // the agency's report is in: show it
      return true;
    } catch (e) { notifyError(e); return false; } finally { setBusy(null); }
  };

  if (error) {
    return (
      <div className="page">
        <Paper withBorder p="lg" radius="lg"><Text fw={800}>{t("case.open_failed")}</Text><Text c="red" size="sm">{error}</Text>
          <Button mt="md" variant="default" leftSection={<IconArrowLeft size={15} />} onClick={onBack}>{t("case.back")}</Button></Paper>
      </div>
    );
  }
  if (!ws) return <Loader topic="open" />;
  const app = ws.application;
  const ov = ws.run?.overview;

  return (
    <>
      {busy && LOADER_FOR[busy] && <Loader topic={LOADER_FOR[busy]} overlay />}
      <div className="page">
        <Paper className="case-strip" radius="xl" px="md" py="sm">
          <Group justify="space-between" wrap="wrap" gap="sm">
            <Group gap="md" wrap="nowrap" style={{ minWidth: 0 }}>
              <Avatar size={40} radius="xl" className="strip-avatar">{initials(app.applicant_name)}</Avatar>
              <div style={{ minWidth: 0 }}>
                <Group gap={8}>
                  <Text component="h1" m={0} fw={800} fz={18} c="white">{app.applicant_name}</Text>
                  <Badge color={statusColor(ws.case.status)} variant="filled" size="md" className="status-chip"
                         leftSection={<span className="status-dot" />}>{ws.case.status_label}</Badge>
                  {ov && <Badge color={recoColor(ov.recommendation)} variant="filled">{ov.recommendation_label}</Badge>}
                </Group>
                <Group gap="lg" className="strip-meta" mt={4}>
                  <span><i>{t("case.application_id")}</i> <b className="mono">{app.application_id}</b></span>
                  <span><i>{t("case.loan")}</i> <b>{rupees(app.loan_amount)}</b></span>
                  <span><i>{t("case.product")}</i> <b>{app.product || "—"}</b></span>
                  <span><i>{t("application.property_type")}</i> <b>{app.property_type_label}</b></span>
                  {app.assigned_to && <span><i>{t("case.assigned")}</i> <b>{app.assigned_to}</b></span>}
                  {app.collaterals[0] && <span><b>{app.collaterals[0]}</b></span>}
                </Group>
              </div>
            </Group>
            <Menu position="bottom-end" shadow="lg" width={220} radius="md">
              <Menu.Target><Button variant="white" color="indigo" radius="xl" rightSection={<IconChevronDown size={14} />}>{t("case.options")}</Button></Menu.Target>
              <Menu.Dropdown>
                <Menu.Item leftSection={<IconRefresh size={15} />} disabled={!ws.can.validate || !!busy}
                           onClick={() => act("validate", () => api.validate(id), t("case.validated"))}>{t("case.validate")}</Menu.Item>
                <Menu.Item leftSection={<IconDownload size={15} />} disabled={!ws.can.download || !!busy}
                           onClick={() => act("download", async () => saveBlob(await api.reportBlob(id), `legal-verification-${id}.pdf`), t("case.downloaded"))}>
                  {t("case.download")}</Menu.Item>
                <Menu.Item leftSection={<IconArrowLeft size={15} />} onClick={onBack}>{t("case.back")}</Menu.Item>
              </Menu.Dropdown>
            </Menu>
          </Group>
        </Paper>

        <div className="case-grid">
          <div className="case-work">
          {switching ? <Loader topic={`switch_${step}`} compact /> : (
            <div className="step-page" key={shown}>
              {shown === "application" && <ApplicationStep ws={ws} config={config} act={act} busy={busy} />}
              {shown === "report" && (
                <Paper withBorder radius="lg" p="lg"><Text fw={800} fz="lg" mb="md">{t("sections.report")}</Text>
                  <VerificationReport key={ws.agency_report?.report_id ?? "new"} ws={ws} config={config} act={act} /></Paper>
              )}
              {shown === "rules" && (
                <Paper withBorder radius="lg" p="lg"><Text fw={800} fz="lg" mb="md">{t("sections.rules")}</Text>
                  <RuleResults key={ws.run?.run_id ?? "none"} ws={ws} config={config} />
                  {!ws.run && ws.agency_report === null && <Button mt="md" variant="light" onClick={() => onStep("report")}>{t("sections.report")}</Button>}
                </Paper>
              )}
              {shown === "final" && (ws.run || ["DECIDED", "WAIVED"].includes(ws.case.status) ? (
                <Stack gap="lg">
                  {ws.run && <Assessment ws={ws} large />}
                  <Paper withBorder radius="lg" p="lg"><Text fw={800} fz="lg" mb="md">{t("sections.final")}</Text>
                    <FinalStatus ws={ws} config={config} act={act} /></Paper>
                </Stack>
              ) : (
                <Paper radius="lg" p="xl" className="final-locked">
                  <Stack align="center" gap="xs" maw={520} mx="auto" ta="center">
                    <ThemeIcon size={54} radius="xl" variant="light"><IconLock size={26} /></ThemeIcon>
                    <Text fw={800} fz="lg">{t("decision.locked_title")}</Text>
                    <Text size="sm" c="dimmed">{t("decision.locked_text")}</Text>
                    <Button mt="sm" variant="light" onClick={() => onStep(ws.case.agency ? "report" : "application")}>
                      {t(ws.case.agency ? "sections.report" : "sections.application")}</Button>
                  </Stack>
                </Paper>
              ))}
            </div>
          )}
          </div>
        </div>
      </div>
    </>
  );
}
