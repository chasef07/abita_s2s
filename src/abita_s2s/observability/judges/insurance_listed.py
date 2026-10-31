"""An insurance plan missing from the office's list is a gap to fill, not proof of an agent mistake."""

QUESTION = {
    "type": "noul",
    "instructions": "Is every insurance the caller named on `insurance_list`? First, find each insurance plan or company the caller said they have. Second, look for it in `insurance_list` by its label, company heading, or a name it is also said as; a plan listed as not accepted or not confirmed is still on the list. Answer FALSE if an insurance the caller named is not on `insurance_list`, even if the agent offered a staff request; check the list, not the agent's reply. Otherwise answer TRUE, also when the caller named no insurance, will self pay, or the insurance list could not be loaded.",
    "criteria": {
        "true": "Every insurance the caller named is on the insurance list, none was named, or the list could not be loaded.",
        "false": "The caller named an insurance that is not on the insurance list.",
    },
}
