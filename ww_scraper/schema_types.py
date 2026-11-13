"""schema.org types of businesses and organisations.

Sites mark themselves up with the most specific subtype (Dentist, BeautySalon,
CafeOrCoffeeShop), rarely with LocalBusiness itself, so a check for "is this an
organisation" has to know the subtypes. The list follows schema.org's LocalBusiness and
Organization trees; names ending in Business, Store, Shop or Organization pass as well.
"""

ORGANIZATION_TYPES = frozenset("""
Organization Corporation NGO OnlineBusiness OnlineStore LocalBusiness Airline Consortium
EducationalOrganization School CollegeOrUniversity GovernmentOrganization MedicalOrganization
NewsMediaOrganization PerformingGroup SportsOrganization SportsTeam WorkersUnion
AnimalShelter ArchiveOrganization AutomotiveBusiness ChildCare Dentist DryCleaningOrLaundry
EmergencyService EmploymentAgency EntertainmentBusiness FinancialService FoodEstablishment
GovernmentOffice HealthAndBeautyBusiness HomeAndConstructionBusiness InternetCafe LegalService
Library LodgingBusiness MedicalBusiness ProfessionalService RadioStation RealEstateAgent
RecyclingCenter SelfStorage ShoppingCenter SportsActivityLocation Store TelevisionStation
TouristInformationCenter TravelAgency
Bakery BarOrPub Brewery CafeOrCoffeeShop Distillery FastFoodRestaurant IceCreamShop Restaurant Winery
BeautySalon DaySpa HairSalon HealthClub NailSalon TattooParlor
CommunityHealth Dermatology DietNutrition Emergency Geriatric Gynecologic MedicalClinic Midwifery
Nursing Obstetric Oncologic Optician Optometric Otolaryngologic Pediatric Pharmacy Physician
Physiotherapy PlasticSurgery Podiatric PrimaryCare Psychiatric PublicHealth Hospital DiagnosticLab
VeterinaryCare
BedAndBreakfast Campground Hostel Hotel Motel Resort VacationRental
AutoBodyShop AutoDealer AutoPartsStore AutoRental AutoRepair AutoWash GasStation
MotorcycleDealer MotorcycleRepair
Electrician GeneralContractor HVACBusiness HousePainter Locksmith MovingCompany Plumber
RoofingContractor
Attorney Notary AccountingService AutomatedTeller BankOrCreditUnion InsuranceAgency
BowlingAlley ExerciseGym GolfCourse PublicSwimmingPool SkiResort SportsClub StadiumOrArena
TennisComplex
AdultEntertainment AmusementPark ArtGallery Casino ComedyClub MovieTheater NightClub
Florist PawnShop
""".split())

_SUFFIXES = ("Business", "Store", "Shop", "Organization")


def is_organization_type(t) -> bool:
    if not isinstance(t, str):
        return False
    t = t.rsplit("/", 1)[-1]          # "https://schema.org/Dentist" -> "Dentist"
    return t in ORGANIZATION_TYPES or t.endswith(_SUFFIXES)
