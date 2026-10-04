type ComingSoonProps = {
  title: string;
  description: string;
  phase: string;
  features: string[];
};

/** Placeholder for pages built in later phases. */
export function ComingSoon({ title, description, phase, features }: ComingSoonProps) {
  return (
    <div className="mx-auto w-full max-w-5xl px-4 py-12 sm:px-6">
      <p className="text-xs font-medium uppercase tracking-wider text-emerald-700 dark:text-emerald-400">{phase}</p>
      <h1 className="mt-2 text-3xl font-semibold tracking-tight">{title}</h1>
      <p className="mt-3 max-w-2xl text-stone-600 dark:text-stone-400">{description}</p>
      <ul className="mt-8 grid gap-3 sm:grid-cols-2">
        {features.map((feature) => (
          <li
            key={feature}
            className="rounded-lg border border-dashed border-stone-300 px-4 py-3 text-sm text-stone-600 dark:border-stone-700 dark:text-stone-400"
          >
            {feature}
          </li>
        ))}
      </ul>
    </div>
  );
}
