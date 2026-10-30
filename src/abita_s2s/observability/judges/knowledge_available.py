"""Missing office knowledge is a gap to fill, not proof of an agent mistake."""

OFFICE_QUESTION = "An office question asks for something this practice would answer the same way for any caller: hours, locations and directions, phones and faxes, practice identifiers, providers and their languages, services, conditions treated, products and programs, self-pay prices, and office policies such as walk-ins, late arrival, cancellations, check-in, referrals the office requires, rides after dilation, forms, how records and prescriptions are sent, and glasses pickup. A question about the caller's own visit counts when an office policy answers it. Not office questions: insurance acceptance, coverage, costs or referral rules; open appointment times; sending, changing or the status of a patient's own records, prescriptions, orders, bills or appointments; medical advice; asking for staff or about the assistant; vendor calls; other businesses."

QUESTION = {
    "type": "noul",
    "instructions": f"Did `office_knowledge` answer the caller's office questions? First, find each question the caller asked. Second, keep the office questions. {OFFICE_QUESTION} Third, answer FALSE if `office_knowledge` did not supply, or said it did not supply, the answer to a kept question, even if the agent guessed or offered staff follow-up; check the knowledge, not the agent's reply. Otherwise answer TRUE, also when office information could not be loaded.",
    "criteria": {
        "true": "Office knowledge answered every office question, none was asked, or office information could not be loaded.",
        "false": "Office knowledge did not supply the answer to an office question.",
    },
}
