/** Test helper: visibility of the synthetic pilot-credential-shaped var. */
try {
  const value = Deno.env.get("VOUCH_PILOT_CREDENTIAL_TEST");
  console.log(value === undefined ? "absent" : "present");
} catch {
  console.log("unreadable");
}
