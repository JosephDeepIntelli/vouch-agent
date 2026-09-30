/** Test helper: reports whether the synthetic sentinel env var is VISIBLE to
 * this process. Run with --allow-env=VOWDO_TEST_ENV_SENTINEL so an absent
 * variable prints "absent" (permission covers the name; absence is real). */
const name = "VOWDO_TEST_ENV_SENTINEL";
try {
  const value = Deno.env.get(name);
  console.log(value === undefined ? "absent" : "present");
} catch {
  console.log("unreadable");
}
