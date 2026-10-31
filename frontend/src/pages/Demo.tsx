import { Link } from 'react-router-dom'

import { Badge, Card } from '@/components/ui'

/** A clearly illustrative scenario. It is static teaching material, never a live camera or event record. */
export function Demo() {
  return (
    <div className="space-y-5">
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <div className="flex items-center gap-2"><Badge className="border-warn/40 bg-warn/12 text-warn">DEMO · illustrative sample</Badge></div>
          <h1 className="mt-2 text-xl font-semibold tracking-tight">How a camera becomes an event</h1>
          <p className="mt-1 max-w-3xl text-sm text-ink-2">This diagram and its example values are synthetic teaching content. They are not a camera feed, model output, or a record from your site.</p>
        </div>
        <Link to="/setup" className="btn-primary">Connect your own camera</Link>
      </header>

      <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_22rem]">
        <Card title="Sample camera view" subtitle="DEMO · no live video" padded={false}>
          <div className="relative aspect-video overflow-hidden rounded-b-lg bg-[#07101b] p-5 sm:p-8" role="img" aria-label="Illustrative demo diagram showing a person sample inside a restricted storage zone">
            <div className="absolute inset-0 opacity-25" style={{ backgroundImage: 'linear-gradient(90deg, transparent 49%, #334155 50%, transparent 51%), linear-gradient(0deg, transparent 49%, #334155 50%, transparent 51%)', backgroundSize: '64px 64px' }} />
            <div className="absolute left-1/2 top-1/2 h-[62%] w-[46%] -translate-x-1/2 -translate-y-1/2 rounded border-2 border-dashed border-crit bg-crit/10" />
            <div className="absolute left-[43%] top-[27%] h-[53%] w-[17%] min-w-16 rounded border-2 border-accent bg-accent/10" />
            <span className="absolute left-[43%] top-[20%] rounded bg-accent px-2 py-1 text-xs font-medium text-bg">Person · Track #12 · 94%</span>
            <span className="absolute left-3 top-3 rounded bg-crit px-2 py-1 text-xs font-semibold text-white">DEMO · SAMPLE BOX</span>
            <span className="absolute bottom-3 left-3 rounded bg-bg/90 px-2 py-1 text-xs text-crit">Restricted Storage Area · sample zone</span>
          </div>
        </Card>

        <Card title="Sample object details" subtitle="Example values only">
          <dl className="grid grid-cols-2 gap-3 text-sm">
            <DemoMetric label="Object" value="Person" />
            <DemoMetric label="Confidence" value="94% (sample)" />
            <DemoMetric label="Track" value="#12 · temporary" />
            <DemoMetric label="Zone" value="Restricted Storage" />
            <DemoMetric label="Status" value="Inside sample zone" />
            <DemoMetric label="Sample time" value="10:24:32 AM" />
          </dl>
          <p className="mt-4 text-xs leading-5 text-ink-2">In a real session, these details come from the active model, current track, configured zone, and server event. A track number is anonymous and lasts only for that camera session.</p>
        </Card>
      </div>

      <Card title="From footage to reviewable activity" subtitle="Illustrative example · not saved to your event history" padded={false}>
        <ol className="grid gap-3 p-4 md:grid-cols-4">
          {[
            ['1 · Camera', 'A configured source provides frames to the VisionOps pipeline.'],
            ['2 · Detection', 'The active model returns supported object labels, boxes, and confidence.'],
            ['3 · Zone check', 'The pipeline compares a tracked object with enabled zone geometry.'],
            ['4 · Event', 'A confirmed zone transition is stored with its timestamp and context.'],
          ].map(([title, description]) => (
            <li key={title} className="rounded-md border border-border bg-surface-2 p-3">
              <h2 className="text-sm font-semibold">{title}</h2>
              <p className="mt-1 text-xs leading-5 text-ink-2">{description}</p>
            </li>
          ))}
        </ol>
        <div className="mx-4 mb-4 rounded-md border border-crit/30 bg-crit/5 p-3 text-sm">
          <span className="font-medium text-crit">DEMO · sample event history</span>
          <p className="mt-1 text-ink-2">10:24:32 AM · Person entered Restricted Storage · Track #12 · sample only</p>
        </div>
      </Card>

      <div className="flex flex-wrap gap-2">
        <Link to="/" className="btn-secondary">Explore dashboard</Link>
        <Link to="/setup" className="btn-primary">Connect your own camera</Link>
        <Link to="/setup" className="btn-ghost">Set up the synthetic fixture</Link>
      </div>
    </div>
  )
}

function DemoMetric({ label, value }: { label: string; value: string }) {
  return <div><dt className="text-[11px] uppercase tracking-wide text-ink-3">{label}</dt><dd className="mt-0.5 text-ink">{value}</dd></div>
}
