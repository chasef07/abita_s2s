"""Missing office knowledge is a gap to fill, not proof of an agent mistake."""

OFFICE_QUESTION = "An office question asks for a fact about this practice that is the same for every caller: hours, locations and directions, phone and fax numbers, providers, services, self-pay fees and prices, and office policies or procedures such as records requests, prescriptions, forms, and glasses or contact lens pickup. These are not office questions: insurance acceptance, coverage, plan referral rules, or costs under a plan; appointment availability or requests to book, change, or cancel; the caller's own appointments, orders, records, prescriptions, bills, or results; medical advice; requests for staff or questions about the assistant; sales or vendor calls; and anything about other businesses."

QUESTION = {
    "type": "noul",
    "instructions": f"Did `office_knowledge` contain the information needed to answer the caller's office questions? {OFFICE_QUESTION} Compare the caller's office questions with that office knowledge, not the agent's answer. Answer FALSE if the office knowledge did not supply, or only said it did not supply, the information an office question needed. Answer TRUE if no office question was asked or office information could not be loaded; a load failure does not prove the knowledge was missing.",
    "criteria": {
        "true": "The office knowledge supplied the information needed to answer the caller's office questions, no office question was asked, or office information could not be loaded.",
        "false": "The office knowledge did not supply the information needed to answer a caller's office question.",
    },
}
