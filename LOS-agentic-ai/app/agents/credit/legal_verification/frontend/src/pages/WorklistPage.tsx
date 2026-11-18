import { Avatar, Badge, Group, Paper, RingProgress, SimpleGrid, Skeleton, Table, Text, TextInput, ThemeIcon } from "@mantine/core";
import { IconAlertTriangle, IconBuildingBank, IconCircleCheck, IconFileText, IconInfoCircle, IconListDetails, IconSearch } from "@tabler/icons-react";
import { useEffect, useState } from "react";
import { recoColor, statusColor } from "../components/kit";
import { ApiError, api } from "../services/api";
import type { Row } from "../types";
import { initials, rupees, t } from "../utils/text";

/** The CPA's worklist: a banner with the validated cases' mix, tiles counted from the list itself, the list. */
export function WorklistPage({ onOpen }: { onOpen: (id: string) => void }) {
  const [query, setQuery] = useState("");
  const [rows, setRows] = useState<Row[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const timer = window.setTimeout(() => {
      api.worklist(query.trim()).then((r) => { setRows(r.applications); setError(null); })
        .catch((e) => setError(e instanceof ApiError ? e.message : t("common.failed")));
    }, 180);
    return () => window.clearTimeout(timer);
  }, [query]);

  const all = rows ?? [];
  const count = (f: (r: Row) => boolean) => all.filter(f).length;
  const reco = { ESCALATE: count((r) => r.recommendation === "ESCALATE"), REVIEW_REQUIRED: count((r) => r.recommendation === "REVIEW_REQUIRED"),
                 CLEAR_FOR_REVIEW: count((r) => r.recommendation === "CLEAR_FOR_REVIEW") };
  const validated = reco.ESCALATE + reco.REVIEW_REQUIRED + reco.CLEAR_FOR_REVIEW;
  const pct = (n: number) => (validated ? (n / validated) * 100 : 0);
  const tiles = [
    { k: "total", icon: IconListDetails, color: "sapphire", n: all.length },
    { k: "new", icon: IconFileText, color: "blue", n: count((r) => ["NEW", "DOCUMENTS_UPLOADED"].includes(r.status)) },
    { k: "agency", icon: IconBuildingBank, color: "teal", n: count((r) => r.status === "WITH_AGENCY") },
    { k: "escalate", icon: IconAlertTriangle, color: "red", n: reco.ESCALATE },
    { k: "review", icon: IconInfoCircle, color: "yellow", n: reco.REVIEW_REQUIRED },
    { k: "clear", icon: IconCircleCheck, color: "green", n: reco.CLEAR_FOR_REVIEW },
  ];

  return (
    <div className="page">
      <Paper className="w-hero" radius="xl" p="xl">
        <Group justify="space-between" wrap="wrap" gap="lg">
          <div style={{ maxWidth: 560 }}>
            <Badge variant="white" color="sapphire" radius="xl" size="sm">{t("worklist.kicker")}</Badge>
            <Text component="h1" fw={800} fz={28} c="white" mt="sm" mb={4} lh={1.2}>{t("worklist.title")}</Text>
            <Text c="sapphire.1" size="sm">{t("worklist.subtitle")}</Text>
            <TextInput mt="md" radius="xl" size="md" leftSection={<IconSearch size={17} />} placeholder={t("worklist.search")}
                       aria-label={t("worklist.search")} value={query} onChange={(e) => setQuery(e.currentTarget.value)} />
          </div>
          <RingProgress size={150} thickness={14} roundCaps
                        sections={[{ value: pct(reco.CLEAR_FOR_REVIEW), color: "green.4" }, { value: pct(reco.REVIEW_REQUIRED), color: "yellow.4" },
                                   { value: pct(reco.ESCALATE), color: "red.4" }]}
                        label={<div style={{ textAlign: "center" }}><Text c="white" fw={800} fz={30} lh={1}>{validated}</Text>
                          <Text c="sapphire.1" size="xs">{t("worklist.validated")}</Text></div>} />
        </Group>
      </Paper>

      <SimpleGrid cols={{ base: 2, sm: 3, lg: 6 }} spacing="md" my="lg">
        {tiles.map((x, i) => (
          <Paper key={x.k} withBorder p="md" className="kpi" style={{ animationDelay: `${i * 0.05}s` }}>
            <ThemeIcon size={34} radius="md" variant="light" color={x.color}><x.icon size={18} /></ThemeIcon>
            <Text fw={800} fz={26} mt="sm" lh={1}>{x.n}</Text>
            <Text size="xs" c="dimmed" fw={600} mt={4}>{t(`worklist.tiles.${x.k}`)}</Text>
          </Paper>
        ))}
      </SimpleGrid>

      <Paper withBorder>
        {error && <Text c="red" p="md">{error}</Text>}
        {!rows && !error && <Skeleton height={220} radius="lg" />}
        {rows && rows.length === 0 && <Text c="dimmed" p="xl" ta="center">{t("worklist.none_text")}</Text>}
        {rows && rows.length > 0 && (
          <Table.ScrollContainer minWidth={760}>
            <Table highlightOnHover verticalSpacing="sm" horizontalSpacing="md">
              <Table.Thead><Table.Tr>
                <Table.Th>{t("worklist.applicant")}</Table.Th><Table.Th>{t("worklist.property")}</Table.Th>
                <Table.Th ta="right">{t("worklist.loan")}</Table.Th><Table.Th>{t("worklist.status")}</Table.Th>
                <Table.Th>{t("worklist.recommendation")}</Table.Th><Table.Th>{t("worklist.agency")}</Table.Th>
              </Table.Tr></Table.Thead>
              <Table.Tbody>
                {rows.map((r) => (
                  <Table.Tr key={r.application_id} className="worklist-row" tabIndex={0} onClick={() => onOpen(r.application_id)}
                            onKeyDown={(e) => { if (e.key === "Enter") onOpen(r.application_id); }}>
                    <Table.Td><Group gap="sm" wrap="nowrap"><Avatar radius="xl" color="sapphire" variant="light">{initials(r.applicant_name)}</Avatar>
                      <div><Text fw={700} size="sm">{r.applicant_name}</Text><Text size="xs" c="dimmed" ff="monospace">{r.application_id}</Text></div></Group></Table.Td>
                    <Table.Td><Text size="sm">{r.property_type_label}</Text></Table.Td>
                    <Table.Td ta="right"><Text size="sm" fw={600}>{rupees(r.loan_amount)}</Text></Table.Td>
                    <Table.Td><Badge color={statusColor(r.status)}>{r.status_label}</Badge></Table.Td>
                    <Table.Td>{r.recommendation ? <Badge color={recoColor(r.recommendation)} variant="dot">{r.recommendation_label}</Badge>
                                                : <Text size="xs" c="dimmed">{t("worklist.not_run")}</Text>}</Table.Td>
                    <Table.Td><Text size="sm" c="dimmed">{r.agency_name ?? "—"}</Text></Table.Td>
                  </Table.Tr>
                ))}
              </Table.Tbody>
            </Table>
          </Table.ScrollContainer>
        )}
      </Paper>
    </div>
  );
}
